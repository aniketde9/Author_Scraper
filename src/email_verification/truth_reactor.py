"""Local SMTP + DNS email verification (Truth Reactor) for Stage 1.5."""

from __future__ import annotations

import random
import re
import socket
import smtplib
import sqlite3
import string
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dns.resolver
import httpx
import structlog
from email_validator import EmailNotValidError, validate_email

from src.config import EmailEnrichmentConfig, _project_root
from src.email_verification.types import VerificationResult

log = structlog.get_logger(__name__)

_cache_lock = threading.Lock()

DISPOSABLE_URL = (
    "https://raw.githubusercontent.com/disposable-email-domains/"
    "disposable-email-domains/main/disposable_email_blocklist.conf"
)
ROLE_URL = "https://raw.githubusercontent.com/mbalatsko/role-based-email-addresses-list/main/list.txt"

FREE_PROVIDERS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "aol.com",
        "protonmail.com",
        "icloud.com",
        "yandex.com",
        "mail.com",
        "zoho.com",
    }
)

PARKED_KEYWORDS = (
    "parking",
    "sedo",
    "godaddy",
    "namecheap",
    "hostgator",
    "bluehost",
    "parked",
    "forwarding",
    "redirect",
)


@dataclass(frozen=True)
class ReactorPaths:
    cache_db: Path
    list_dir: Path


def default_reactor_paths() -> ReactorPaths:
    data = _project_root() / "data"
    return ReactorPaths(cache_db=data / "email_reactor_cache.db", list_dir=data / "lists")


class EmailTruthReactor:
    """Hardened local verifier: syntax, DNS, risk lists, SMTP, confidence."""

    @classmethod
    def from_enrichment_config(cls, cfg: EmailEnrichmentConfig) -> EmailTruthReactor:
        data = _project_root() / "data"
        cache: Path | None = Path(cfg.reactor_cache_db) if (cfg.reactor_cache_db or "").strip() else None
        ldir: Path | None = Path(cfg.reactor_list_dir) if (cfg.reactor_list_dir or "").strip() else None
        return cls(
            cache_db=cache,
            list_dir=ldir,
            list_update_interval_seconds=cfg.reactor_list_update_interval_seconds,
            smtp_timeout=cfg.smtp_timeout_seconds,
            smtp_catchall_timeout=cfg.smtp_catchall_timeout_seconds,
        )

    def __init__(
        self,
        *,
        cache_db: Path | None = None,
        list_dir: Path | None = None,
        list_update_interval_seconds: float = 7 * 86400,
        smtp_timeout: float = 15.0,
        smtp_catchall_timeout: float = 12.0,
        smtp_greylist_wait: bool = True,
        smtp_catchall_probe: bool = True,
    ) -> None:
        paths = default_reactor_paths()
        self._cache_db = (cache_db or paths.cache_db).resolve()
        self._list_dir = (list_dir or paths.list_dir).resolve()
        self._list_update_interval = list_update_interval_seconds
        self._smtp_timeout = smtp_timeout
        self._catchall_timeout = smtp_catchall_timeout
        self._smtp_greylist_wait = smtp_greylist_wait
        self._smtp_catchall_probe = smtp_catchall_probe
        self._init_db()
        self._disposable_path = self._download_list(DISPOSABLE_URL, "disposable.txt")
        self._role_path = self._download_list(ROLE_URL, "role.txt")
        self._disposable = self._load_set(self._disposable_path)
        self._role = self._load_set(self._role_path)

    def _init_db(self) -> None:
        self._cache_db.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock:
            conn = sqlite3.connect(self._cache_db, check_same_thread=False, timeout=30.0)
            try:
                c = conn.cursor()
                c.execute(
                    """CREATE TABLE IF NOT EXISTS mx_cache (
                    mx_host TEXT PRIMARY KEY,
                    greylist_delay REAL,
                    catch_all_rate REAL,
                    seg_probability REAL,
                    last_checked TEXT
                )"""
                )
                conn.commit()
            finally:
                conn.close()

    def _download_list(self, url: str, filename: str) -> Path:
        self._list_dir.mkdir(parents=True, exist_ok=True)
        path = self._list_dir / filename
        if path.exists() and (time.time() - path.stat().st_mtime) < self._list_update_interval:
            return path
        log.info("reactor_list_update", file=filename)
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(url)
                r.raise_for_status()
            path.write_text(r.text, encoding="utf-8")
            log.info("reactor_list_ok", file=filename)
        except Exception as e:
            log.warning("reactor_list_failed", file=filename, error=str(e))
        return path

    def _load_set(self, path: Path) -> set[str]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            return {line.strip().lower() for line in text.splitlines() if line.strip() and not line.startswith("#")}
        except OSError:
            return set()

    def _get_mx_cache(self, mx_host: str) -> dict[str, Any] | None:
        with _cache_lock:
            conn = sqlite3.connect(self._cache_db, check_same_thread=False, timeout=30.0)
            try:
                c = conn.cursor()
                c.execute("SELECT * FROM mx_cache WHERE mx_host=?", (mx_host,))
                row = c.fetchone()
            finally:
                conn.close()
        if not row:
            return None
        return {
            "greylist_delay": row[1],
            "catch_all_rate": row[2],
            "seg_probability": row[3],
            "last_checked": row[4],
        }

    def _save_mx_cache(
        self,
        mx_host: str,
        greylist_delay: float | None,
        catch_all_rate: float,
        seg_probability: float,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with _cache_lock:
            conn = sqlite3.connect(self._cache_db, check_same_thread=False, timeout=30.0)
            try:
                c = conn.cursor()
                c.execute(
                    """INSERT OR REPLACE INTO mx_cache
                    (mx_host, greylist_delay, catch_all_rate, seg_probability, last_checked)
                    VALUES (?, ?, ?, ?, ?)""",
                    (mx_host, greylist_delay, catch_all_rate, seg_probability, now),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _syntax_check(email: str) -> tuple[bool, str | None, str | None, str | None]:
        try:
            valid = validate_email(email, check_deliverability=False)
            return True, valid.normalized, valid.local_part, valid.domain
        except EmailNotValidError as e:
            return False, str(e), None, None

    @staticmethod
    def _get_mx_records(domain: str) -> list[Any] | None:
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            return sorted(answers, key=lambda r: r.preference)
        except Exception:
            return None

    @staticmethod
    def _is_parked_mx(mx_records: list[Any] | None) -> bool:
        if not mx_records:
            return True
        for mx in mx_records:
            host = str(mx.exchange).rstrip(".").lower()
            if any(k in host for k in PARKED_KEYWORDS):
                return True
        return False

    def _is_role_local(self, local: str | None) -> bool:
        if not local:
            return False
        return local.lower() in self._role

    def _is_disposable(self, domain: str | None) -> bool:
        if not domain:
            return False
        return domain.lower() in self._disposable

    @staticmethod
    def _is_free_provider(domain: str | None) -> bool:
        if not domain:
            return False
        return domain.lower() in FREE_PROVIDERS

    def _smtp_probe(self, mx_host: str, email: str, timeout: float) -> tuple[bool, int | None, str, float]:
        start = time.perf_counter()
        try:
            with smtplib.SMTP(timeout=timeout) as server:
                server.set_debuglevel(0)
                server.connect(mx_host, 25)
                server.helo(socket.getfqdn() or "localhost")
                server.mail("verify@truth-reactor.local")
                code, msg = server.rcpt(email)
                server.quit()
            duration = time.perf_counter() - start
            m = (msg or b"")
            m_str = m.decode(errors="ignore") if isinstance(m, (bytes, bytearray)) else str(m)
            return (code == 250), int(code) if code is not None else None, m_str, duration
        except Exception as e:
            duration = time.perf_counter() - start
            return False, None, str(e), duration

    def _probe_catch_all_and_seg(
        self, mx_host: str, domain: str, real_email: str, timeout: float
    ) -> dict[str, Any]:
        fakes = [
            "".join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 20))) + f"@{domain}",
            "".join(random.choices(string.ascii_lowercase, k=12)) + f"@{domain}",
            "test1234567890" + f"@{domain}",
            "admin" + f"@{domain}",
            "info" + f"@{domain}",
        ]
        results: list[dict[str, Any]] = []
        for addr in [real_email] + fakes:
            acc, code, msg, duration = self._smtp_probe(mx_host, addr, timeout)
            results.append({"addr": addr, "accepts": acc, "code": code, "duration": duration, "msg": msg})
            time.sleep(0.3)

        real_r = results[0]
        fakes_r = results[1:]
        accept_rate = sum(r["accepts"] for r in fakes_r) / max(len(fakes_r), 1)
        durations = [r["duration"] for r in results]
        timing_var = (max(durations) - min(durations)) if durations else 0.0
        is_catch_all = accept_rate > 0.75
        is_seg_like = timing_var < 0.4 and accept_rate > 0.6
        return {
            "catch_all_rate": accept_rate,
            "is_catch_all": is_catch_all,
            "is_seg_like": is_seg_like,
            "timing_variance": timing_var,
            "results": results,
            "real": real_r,
        }

    @staticmethod
    def _calculate_confidence(signals: dict[str, Any]) -> tuple[float, str, list[str]]:
        score = 65.0
        breakdown: list[str] = []
        if signals.get("syntax_valid"):
            score += 18
            breakdown.append("Syntax: +18")
        if signals.get("mx_valid"):
            score += 12
            breakdown.append("MX valid: +12")
        if signals.get("smtp_250_after_retries"):
            score += 25
            breakdown.append("SMTP 250: +25")
        smtp_code = signals.get("smtp_code")
        smtp_accepts = bool(signals.get("smtp_250_after_retries"))
        if not smtp_accepts:
            if isinstance(smtp_code, int) and 500 <= smtp_code < 600:
                score -= 45
                breakdown.append("SMTP hard reject (5xx): -45")
            elif isinstance(smtp_code, int) and 400 <= smtp_code < 500:
                score -= 20
                breakdown.append("SMTP transient (4xx): -20")
            else:
                score -= 15
                breakdown.append("SMTP no definitive accept: -15")
        car = float(signals.get("catch_all_rate") or 0.0)
        if car < 0.3:
            score += 10
            breakdown.append("Low catch-all: +10")
        elif car > 0.75:
            score -= 30
            breakdown.append("High catch-all: -30")
        if signals.get("seg_like"):
            score -= 22
            breakdown.append("SEG detected: -22")
        if signals.get("role"):
            score -= 15
            breakdown.append("Role account: -15")
        if signals.get("disposable"):
            score = 5.0
            breakdown.append("Disposable: hard fail")
        if signals.get("parked"):
            score = 10.0
            breakdown.append("Parked MX: hard fail")

        conf = max(0.0, min(100.0, round(score, 1)))
        margin = 5.0 if conf > 85 else 12.0
        conf_str = f"{int(conf)}% ± {int(margin)}%"
        return conf, conf_str, breakdown

    @staticmethod
    def _classify_end(
        confidence: float,
        accepts: bool,
        is_catch_all: bool,
        smtp_code: int | None,
    ) -> tuple[str, str]:
        if isinstance(smtp_code, int) and 500 <= smtp_code < 600 and not is_catch_all:
            return "invalid", "Invalid — SMTP hard reject (5xx)"
        if confidence >= 90 and accepts and not is_catch_all:
            return "deliverable", "Deliverable — safe to treat as mailbox likely valid"
        if confidence >= 70:
            return "risky", "Risky — review before sending"
        if confidence < 60 or (not accepts):
            return "invalid", "Invalid or high risk"
        return "unknown", "Unknown — ambiguous signals"

    def verify(
        self,
        email: str,
        *,
        do_smtp: bool = True,
    ) -> VerificationResult:
        ts = datetime.now(timezone.utc).isoformat()
        base: dict[str, Any] = {"input": email, "timestamp": ts}
        out = VerificationResult(input=email, timestamp=ts, raw_report=base)

        ok, normalized, local, domain = self._syntax_check(email)
        out.syntax_valid = ok
        out.normalized = normalized if ok else None
        out.local_part = local
        out.domain = domain
        if not ok:
            out.syntax_error = str(normalized)
            out.category = "invalid"
            out.final_label = "Invalid syntax"
            out.confidence = 0.0
            out.confidence_range = "0% ± 0%"
            return out

        assert normalized is not None
        assert domain is not None

        mx_records = self._get_mx_records(domain)
        out.mx_valid = bool(mx_records)
        out.parked = self._is_parked_mx(mx_records) if mx_records else True
        out.role = self._is_role_local(local)
        out.disposable = self._is_disposable(domain)
        out.free_provider = self._is_free_provider(domain)

        base["dns"] = {
            "mx_valid": out.mx_valid,
            "parked": out.parked,
        }
        base["risk"] = {
            "role": out.role,
            "disposable": out.disposable,
            "free_provider": out.free_provider,
        }

        if not out.mx_valid or out.parked or out.disposable:
            signals: dict[str, Any] = {
                "syntax_valid": True,
                "mx_valid": out.mx_valid,
                "smtp_250_after_retries": False,
                "catch_all_rate": 1.0,
                "seg_like": False,
                "role": out.role,
                "disposable": out.disposable,
                "parked": out.parked,
            }
            c, cstr, brk = self._calculate_confidence(signals)
            out.confidence = c
            out.confidence_range = cstr
            out.breakdown = brk
            out.category = "invalid"
            out.final_label = "Invalid (DNS, parked, or disposable)"
            return out

        if not do_smtp:
            out.smtp_not_run = True
            out.category = "unknown"
            out.final_label = "DNS/lists only (SMTP disabled)"
            c, cstr, brk = self._calculate_confidence(
                {
                    "syntax_valid": True,
                    "mx_valid": True,
                    "smtp_250_after_retries": False,
                    "catch_all_rate": 0.0,
                    "seg_like": False,
                    "role": out.role,
                    "disposable": False,
                    "parked": False,
                }
            )
            out.confidence = c
            out.confidence_range = cstr
            out.breakdown = brk
            return out

        assert mx_records is not None
        primary_mx = str(mx_records[0].exchange).rstrip(".")
        out.primary_mx = primary_mx
        cache = self._get_mx_cache(primary_mx)

        accepts, code, msg, duration = self._smtp_probe(primary_mx, normalized, self._smtp_timeout)
        out.smtp_code = code
        out.smtp_message = (msg or "")[:500]
        out.smtp_duration_sec = duration
        if (
            self._smtp_greylist_wait
            and code is not None
            and 400 <= code < 500
            and cache
            and cache.get("greylist_delay")
        ):
            delay = float(cache["greylist_delay"] or 45.0)
            log.info("reactor_greylist_wait", seconds=delay, mx=primary_mx)
            time.sleep(delay + random.uniform(0, 10.0))
            accepts, code, msg, duration = self._smtp_probe(primary_mx, normalized, self._smtp_timeout)
            out.smtp_code = code
            out.smtp_message = (msg or "")[:500]
            out.smtp_duration_sec = duration

        try:
            if self._smtp_catchall_probe:
                deep = self._probe_catch_all_and_seg(primary_mx, domain, normalized, self._catchall_timeout)
            else:
                deep = {
                    "catch_all_rate": 0.25,
                    "is_catch_all": False,
                    "is_seg_like": False,
                    "timing_variance": 0.0,
                    "results": [],
                }
        except Exception as e:
            log.warning("reactor_catchall_probe_failed", error=str(e), mx=primary_mx)
            deep = {
                "catch_all_rate": 0.5,
                "is_catch_all": False,
                "is_seg_like": False,
                "timing_variance": 0.0,
                "results": [],
            }
            out.error = str(e)

        out.smtp_accepts = accepts
        out.catch_all_rate = float(deep["catch_all_rate"])
        out.is_catch_all = bool(deep["is_catch_all"])
        out.is_seg_like = bool(deep["is_seg_like"])
        out.timing_variance = float(deep["timing_variance"])
        gdelay = float(duration) if (code is not None and 400 <= code < 500) else 30.0
        self._save_mx_cache(
            primary_mx,
            greylist_delay=gdelay,
            catch_all_rate=out.catch_all_rate,
            seg_probability=1.0 if out.is_seg_like else 0.15,
        )

        signals2: dict[str, Any] = {
            "syntax_valid": True,
            "mx_valid": True,
            "smtp_250_after_retries": bool(accepts),
            "smtp_code": code,
            "catch_all_rate": out.catch_all_rate,
            "seg_like": out.is_seg_like,
            "role": out.role,
            "disposable": out.disposable,
            "parked": out.parked,
        }
        c, cstr, brk = self._calculate_confidence(signals2)
        out.confidence = c
        out.confidence_range = cstr
        out.breakdown = brk
        out.category, out.final_label = self._classify_end(c, bool(accepts), out.is_catch_all, code)
        if out.is_seg_like and out.category == "deliverable":
            out.category = "risky"
        base["smtp"] = {
            "mx_used": primary_mx,
            "accepts": accepts,
            "code": code,
            "catch_all_rate": out.catch_all_rate,
            "is_seg_like": out.is_seg_like,
            "timing_variance": out.timing_variance,
        }
        return out

    def verify_safe(self, email: str, *, do_smtp: bool = True) -> VerificationResult:
        try:
            return self.verify(email, do_smtp=do_smtp)
        except Exception as e:
            log.exception("reactor_verify_failed", email=email, error=str(e))
            r = VerificationResult(
                input=email,
                timestamp=datetime.now(timezone.utc).isoformat(),
                syntax_valid=False,
                error=str(e),
                category="unknown",
                final_label="Reactor error",
            )
            return r
