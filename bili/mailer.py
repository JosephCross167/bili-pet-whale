"""发邮件。收件人固定是 [targets].report_mail，**不接受调用方传收件人**。

这是有意的：任何"顺手发给别人"的需求都必须改代码，而不是改一个参数。
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from . import config as cfgmod
from .logs import get

log = get("mail")


class MailError(RuntimeError):
    pass


def send(cfg: cfgmod.Config, subject: str, body: str) -> bool:
    mail = cfg.mail
    if not mail.enabled:
        log.info("邮件未启用（[mail].enabled = false），跳过")
        return False
    if not mail.ready:
        raise MailError("邮件配置不完整：需要 [mail].server 和 secrets.toml 的 username / password")

    message = EmailMessage()
    # policy.default 会把非 ASCII 主题按 RFC 2047 编码；不要手动套 Header（会 TypeError）
    message["Subject"] = subject
    message["From"] = mail.username
    message["To"] = cfg.targets.report_mail
    message.set_content(body, subtype="plain", charset="utf-8")

    context = ssl.create_default_context()
    try:
        if mail.use_ssl:
            with smtplib.SMTP_SSL(mail.server, mail.port, timeout=25, context=context) as server:
                server.login(mail.username, mail.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(mail.server, mail.port, timeout=25) as server:
                server.starttls(context=context)
                server.login(mail.username, mail.password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        # 不回显服务端原文：那里面可能带账号信息
        raise MailError("SMTP 认证失败（授权码不对，或该邮箱没开 SMTP 服务）") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"发信失败：{type(exc).__name__}") from None

    log.info("报告已发到 %s：%s", cfg.targets.report_mail, subject)
    return True
