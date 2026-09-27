# -*- coding: utf-8 -*-
"""L1 官方 AI 总结真机探针（只读）。

在真机插件目录运行:
    python tests/probe_conclusion.py BV1Sq4y167Rj
自动读取本插件 config.toml 的 credential.sessdata，依次测试:
  1. view 接口（无需签名）取 cid/标题
  2. nav 接口验证登录态
  3. conclusion/get（WBI 签名 + SESSDATA）打印完整响应
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bili_video import API_BASE, BiliVideoClient, VideoTarget  # noqa: E402


def load_sessdata() -> str:
    """从插件 config.toml 读 SESSDATA（简单行解析，不引第三方库）。"""
    cfg = Path(__file__).resolve().parent.parent / "config.toml"
    if not cfg.exists():
        print(f"[!] 未找到 {cfg}（真机需先让插件生成配置并填入 sessdata）")
        return ""
    for line in cfg.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("sessdata"):
            v = s.split("=", 1)[1].strip().strip('"').strip("'")
            return v
    return ""


async def main(bvid: str) -> None:
    sessdata = load_sessdata()
    print(f"[*] SESSDATA: {'已配置(' + str(len(sessdata)) + '字符)' if sessdata else '未配置'}")
    client = BiliVideoClient(sessdata=sessdata, timeout_sec=15.0)
    try:
        # 1. view 取 cid
        target = VideoTarget(bvid)
        info = await client.get_video_info(target)
        print(f"[1] view OK: title={info['title'][:40]} cid={info['cid']} "
              f"up_mid={info['owner_mid']}")

        # 2. nav 验证登录态（isLogin + mid）
        nav = await client._get_json(f"{API_BASE}/x/web-interface/nav")
        nd = nav.get("data") or {}
        print(f"[2] nav: isLogin={nd.get('isLogin')} mid={nd.get('mid')} "
              f"code={nav.get('code')}")

        # 3. conclusion/get（签名 + SESSDATA）
        result = await client.get_ai_conclusion(info)
        print(f"[3] conclusion: {'命中' if result else '未命中'}")
        print(f"    失败原因: {client.last_conclusion_error}")
        if result:
            print(f"    summary: {result['summary'][:120]}…")
            print(f"    outline 段数: {len(result['outline'])}")
    finally:
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python tests/probe_conclusion.py <BV号>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
