"""监控层：告警的路由与投递。

这一层不认识 HTTP 客户端以外的世界（`requests`/`smtplib`/`subprocess` 的落点在
`monitor/notifiers`），也不认识 CLI：入口在 `bootstrap` 装配好之后把一条告警交给它。
"""
