"""
Freemail 邮箱服务实现
基于自部署 Cloudflare Worker 临时邮箱服务 (https://github.com/idinging/freemail)
"""

import re
import time
import logging
import random
import string
from datetime import datetime, timezone
from html import unescape
from typing import Optional, Dict, Any, List

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..core.http_client import HTTPClient, RequestConfig
from ..config.constants import OTP_CODE_PATTERN, OTP_CODE_SEMANTIC_PATTERN

logger = logging.getLogger(__name__)


class FreemailService(BaseEmailService):
    """
    Freemail 邮箱服务
    基于自部署 Cloudflare Worker 的临时邮箱
    """

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        """
        初始化 Freemail 服务

        Args:
            config: 配置字典，支持以下键:
                - base_url: Worker 域名地址 (必需)
                - admin_token: Admin Token，对应 JWT_TOKEN (必需)
                - domain: 邮箱域名，如 example.com
                - timeout: 请求超时时间，默认 30
                - max_retries: 最大重试次数，默认 3
            name: 服务名称
        """
        super().__init__(EmailServiceType.FREEMAIL, name)

        required_keys = ["base_url", "admin_token"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        default_config = {
            "timeout": 30,
            "max_retries": 3,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = self.config["base_url"].rstrip("/")

        http_config = RequestConfig(
            timeout=self.config["timeout"],
            max_retries=self.config["max_retries"],
        )
        self.http_client = HTTPClient(proxy_url=None, config=http_config)

        # 缓存 domain 列表
        self._domains = []
        self._last_used_mail_ids: Dict[str, str] = {}

    def _get_headers(self) -> Dict[str, str]:
        """构造 admin 请求头"""
        return {
            "Authorization": f"Bearer {self.config['admin_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _make_request(self, method: str, path: str, **kwargs) -> Any:
        """
        发送请求并返回 JSON 数据

        Args:
            method: HTTP 方法
            path: 请求路径（以 / 开头）
            **kwargs: 传递给 http_client.request 的额外参数

        Returns:
            响应 JSON 数据

        Raises:
            EmailServiceError: 请求失败
        """
        url = f"{self.config['base_url']}{path}"
        kwargs.setdefault("headers", {})
        kwargs["headers"].update(self._get_headers())

        try:
            response = self.http_client.request(method, url, **kwargs)

            if response.status_code >= 400:
                error_msg = f"请求失败: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"{error_msg} - {error_data}"
                except Exception:
                    error_msg = f"{error_msg} - {response.text[:200]}"
                self.update_status(False, EmailServiceError(error_msg))
                raise EmailServiceError(error_msg)

            try:
                return response.json()
            except Exception:
                return {"raw_response": response.text}

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"请求失败: {method} {path} - {e}")

    def _ensure_domains(self):
        """获取并缓存可用域名列表"""
        if not self._domains:
            try:
                domains = self._make_request("GET", "/api/domains")
                if isinstance(domains, list):
                    self._domains = domains
            except Exception as e:
                logger.warning(f"获取 Freemail 域名列表失败: {e}")

    def _strip_html(self, value: Any) -> str:
        text = str(value or "")
        return unescape(re.sub(r"<[^>]+>", " ", text))

    def _parse_timestamp(self, value: Any) -> Optional[float]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            return ts if ts > 0 else None

        text = str(value).strip()
        if not text:
            return None

        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            ts = float(text)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            return ts if ts > 0 else None

        candidates = [text]
        if "T" not in text and " " in text:
            candidates.append(text.replace(" ", "T", 1))

        for candidate in candidates:
            normalized = candidate.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(normalized)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).timestamp()
            except Exception:
                continue

        return None

    def _extract_mail_timestamp(self, mail: Dict[str, Any]) -> Optional[float]:
        for key in ("received_at", "receivedAt", "created_at", "createdAt", "timestamp", "date"):
            ts = self._parse_timestamp(mail.get(key))
            if ts is not None:
                return ts
        return None

    def _extract_mail_id(self, mail: Dict[str, Any], fallback_index: int = 0) -> str:
        for key in ("id", "mail_id", "_id"):
            value = str(mail.get(key) or "").strip()
            if value:
                return value

        sender = str(mail.get("sender") or mail.get("from") or "").strip()
        subject = str(mail.get("subject") or "").strip()
        preview = str(mail.get("preview") or mail.get("snippet") or "").strip()
        return f"fallback-{fallback_index}-{sender}-{subject}-{preview[:64]}"

    def _extract_mail_fields(self, mail: Dict[str, Any]) -> Dict[str, str]:
        sender = str(mail.get("sender") or mail.get("from") or "").strip()
        subject = str(mail.get("subject") or "").strip()
        preview = self._strip_html(mail.get("preview") or mail.get("snippet") or "")
        text_body = self._strip_html(mail.get("content") or mail.get("text") or "")
        html_body = self._strip_html(mail.get("html_content") or mail.get("html") or "")
        return {
            "sender": sender,
            "subject": subject,
            "body": "\n".join(part for part in [preview, text_body, html_body] if part).strip(),
        }

    def _is_openai_otp_mail(self, sender: str, subject: str, body: str) -> bool:
        sender_l = str(sender or "").lower()
        subject_l = str(subject or "").lower()
        body_l = str(body or "").lower()
        blob = f"{sender_l}\n{subject_l}\n{body_l}"

        if "openai" not in sender_l and "openai" not in blob:
            return False

        otp_keywords = (
            "verification code",
            "verify",
            "one-time code",
            "one time code",
            "otp",
            "log in",
            "login",
            "security code",
            "验证码",
        )
        return any(keyword in blob for keyword in otp_keywords)

    def _extract_otp_code(self, content: str, pattern: str) -> tuple[Optional[str], bool]:
        text = str(content or "")
        if not text:
            return None, False

        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1), True

        simple_match = re.search(pattern, text)
        if simple_match:
            return simple_match.group(1), False

        return None, False

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        通过 API 创建临时邮箱

        Returns:
            包含邮箱信息的字典:
            - email: 邮箱地址
            - service_id: 同 email（用作标识）
        """
        self._ensure_domains()
        
        req_config = config or {}
        domain_index = 0
        target_domain = req_config.get("domain") or self.config.get("domain")
        
        if target_domain and self._domains:
            for i, d in enumerate(self._domains):
                if d == target_domain:
                    domain_index = i
                    break
                    
        prefix = req_config.get("name")
        try:
            if prefix:
                body = {
                    "local": prefix,
                    "domainIndex": domain_index
                }
                resp = self._make_request("POST", "/api/create", json=body)
            else:
                params = {"domainIndex": domain_index}
                length = req_config.get("length")
                if length:
                    params["length"] = length
                resp = self._make_request("GET", "/api/generate", params=params)

            email = resp.get("email")
            if not email:
                raise EmailServiceError(f"创建邮箱失败，未返回邮箱地址: {resp}")

            email_info = {
                "email": email,
                "service_id": email,
                "id": email,
                "created_at": time.time(),
            }

            logger.info(f"成功创建 Freemail 邮箱: {email}")
            self.update_status(True)
            return email_info

        except Exception as e:
            self.update_status(False, e)
            if isinstance(e, EmailServiceError):
                raise
            raise EmailServiceError(f"创建邮箱失败: {e}")

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """
        从 Freemail 邮箱获取验证码

        Args:
            email: 邮箱地址
            email_id: 未使用，保留接口兼容
            timeout: 超时时间（秒）
            pattern: 验证码正则
            otp_sent_at: OTP 发送时间戳（暂未使用）

        Returns:
            验证码字符串，超时返回 None
        """
        logger.info(f"正在从 Freemail 邮箱 {email} 获取验证码...")

        start_time = time.time()
        seen_mail_ids: set = set()
        email_key = str(email or "").strip().lower()
        last_used_mail_id = self._last_used_mail_ids.get(email_key)
        unknown_ts_grace_seconds = 15

        while time.time() - start_time < timeout:
            try:
                mails = self._make_request("GET", "/api/emails", params={"mailbox": email, "limit": 20})
                if not isinstance(mails, list):
                    time.sleep(3)
                    continue

                candidates: List[Dict[str, Any]] = []
                unknown_ts_candidates: List[Dict[str, Any]] = []

                for index, mail in enumerate(mails):
                    mail_id = self._extract_mail_id(mail, fallback_index=index)
                    if mail_id in seen_mail_ids:
                        continue
                    if last_used_mail_id and mail_id == last_used_mail_id:
                        continue

                    seen_mail_ids.add(mail_id)

                    mail_ts = self._extract_mail_timestamp(mail)
                    if otp_sent_at and mail_ts is not None and mail_ts + 2 < otp_sent_at:
                        continue

                    parsed = self._extract_mail_fields(mail)
                    sender = parsed["sender"]
                    subject = parsed["subject"]
                    body = parsed["body"]
                    content = f"{sender}\n{subject}\n{body}".strip()

                    looks_like_openai_otp = self._is_openai_otp_mail(sender, subject, body)
                    verification_code = str(mail.get("verification_code") or "").strip()
                    if verification_code and re.fullmatch(r"\d{6}", verification_code):
                        code = verification_code
                        semantic_hit = True
                    else:
                        code, semantic_hit = self._extract_otp_code(content, pattern)

                    if (not looks_like_openai_otp) or (not code):
                        try:
                            detail = self._make_request("GET", f"/api/email/{mail_id}")
                            if isinstance(detail, dict):
                                detail_ts = self._extract_mail_timestamp(detail)
                                if detail_ts is not None:
                                    mail_ts = detail_ts
                                    if otp_sent_at and mail_ts + 2 < otp_sent_at:
                                        continue

                                detail_parsed = self._extract_mail_fields(detail)
                                sender = detail_parsed["sender"] or sender
                                subject = detail_parsed["subject"] or subject
                                body = detail_parsed["body"] or body
                                content = f"{sender}\n{subject}\n{body}".strip()
                                looks_like_openai_otp = self._is_openai_otp_mail(sender, subject, body)

                                detail_code = str(detail.get("verification_code") or "").strip()
                                if detail_code and re.fullmatch(r"\d{6}", detail_code):
                                    code = detail_code
                                    semantic_hit = True
                                else:
                                    code, semantic_hit = self._extract_otp_code(content, pattern)
                        except Exception as e:
                            logger.debug(f"获取 Freemail 邮件详情失败: {e}")

                    if (not looks_like_openai_otp) or (not code):
                        continue

                    candidate = {
                        "mail_id": mail_id,
                        "code": str(code),
                        "mail_ts": mail_ts,
                        "semantic_hit": bool(semantic_hit),
                        "is_recent": bool(
                            otp_sent_at and (mail_ts is not None) and (mail_ts + 2 >= otp_sent_at)
                        ),
                    }
                    if otp_sent_at and mail_ts is None:
                        unknown_ts_candidates.append(candidate)
                    else:
                        candidates.append(candidate)

                elapsed = time.time() - start_time
                if otp_sent_at and (not candidates) and unknown_ts_candidates and elapsed < unknown_ts_grace_seconds:
                    time.sleep(3)
                    continue

                all_candidates = candidates + unknown_ts_candidates
                if all_candidates:
                    best = sorted(
                        all_candidates,
                        key=lambda item: (
                            1 if item.get("is_recent") else 0,
                            1 if item.get("mail_ts") is not None else 0,
                            float(item.get("mail_ts") or 0.0),
                            1 if item.get("semantic_hit") else 0,
                        ),
                        reverse=True,
                    )[0]
                    code = str(best["code"])
                    self._last_used_mail_ids[email_key] = str(best["mail_id"])
                    logger.info(
                        "从 Freemail 邮箱 %s 找到验证码: %s（mail_id=%s ts=%s semantic=%s）",
                        email,
                        code,
                        best["mail_id"],
                        best.get("mail_ts"),
                        best.get("semantic_hit"),
                    )
                    self.update_status(True)
                    return code

            except Exception as e:
                logger.debug(f"检查 Freemail 邮件时出错: {e}")

            time.sleep(3)

        logger.warning(f"等待 Freemail 验证码超时: {email}")
        return None

    def list_emails(self, **kwargs) -> List[Dict[str, Any]]:
        """
        列出邮箱

        Args:
            **kwargs: 额外查询参数

        Returns:
            邮箱列表
        """
        try:
            params = {
                "limit": kwargs.get("limit", 100),
                "offset": kwargs.get("offset", 0)
            }
            resp = self._make_request("GET", "/api/mailboxes", params=params)
            
            emails = []
            if isinstance(resp, list):
                for mail in resp:
                    address = mail.get("address")
                    if address:
                        emails.append({
                            "id": address,
                            "service_id": address,
                            "email": address,
                            "created_at": mail.get("created_at"),
                            "raw_data": mail
                        })
            self.update_status(True)
            return emails
        except Exception as e:
            logger.warning(f"列出 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return []

    def delete_email(self, email_id: str) -> bool:
        """
        删除邮箱
        """
        try:
            self._make_request("DELETE", "/api/mailboxes", params={"address": email_id})
            logger.info(f"已删除 Freemail 邮箱: {email_id}")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"删除 Freemail 邮箱失败: {e}")
            self.update_status(False, e)
            return False

    def check_health(self) -> bool:
        """检查服务健康状态"""
        try:
            self._make_request("GET", "/api/domains")
            self.update_status(True)
            return True
        except Exception as e:
            logger.warning(f"Freemail 健康检查失败: {e}")
            self.update_status(False, e)
            return False
