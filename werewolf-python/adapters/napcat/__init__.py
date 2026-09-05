"""NapCat（OneBot 11）接入层。

本项目的唯一 QQ 接入方案。相较官方开放平台 Bot：
用户标识就是真实 QQ 号（群聊与私聊完全一致，不需要任何绑定握手），
可以主动发消息、可以读群成员列表、机器人是群管时还能改群名片。
"""

from .adapter import NapCatAdapter, NapCatCallError

__all__ = ["NapCatAdapter", "NapCatCallError"]
