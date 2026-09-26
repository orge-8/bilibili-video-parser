"""真机实测：用插件的 BiliVideoClient 跑一次完整降级链（只读诊断，不注入）。

    devkit venv python tests/real_probe.py BV16se36sEQk
"""
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bili_video import BiliVideoClient, VideoTarget, format_count, format_duration


async def main() -> int:
    target_arg = sys.argv[1] if len(sys.argv) > 1 else "BV16se36sEQk"
    target = VideoTarget(target_arg) if not target_arg.startswith("http") \
        else VideoTarget("BV16se36sEQk")
    client = BiliVideoClient(sessdata="", timeout_sec=20.0)
    print(f"== 目标: {target.video_id} ==")
    try:
        # L3 基础信息
        info = await client.get_video_info(target)
        print(f"[L3] 标题: {info['title']}")
        print(f"[L3] UP主: {info['owner']} (mid={info['owner_mid']})")
        print(f"[L3] 时长: {format_duration(info['duration'])}  分P: {info['page']}/{info['pages']}")
        print(f"[L3] cid={info['cid']} aid={info['aid']}")
        print(f"[L3] 播放 {format_count(info['view'])} 弹幕 {format_count(info['danmaku'])} "
              f"点赞 {format_count(info['like'])}")
        print(f"[L3] 简介: {info['desc'][:80]}{'…' if len(info['desc']) > 80 else ''}")

        # L1 官方 AI 总结
        conclusion = await client.get_ai_conclusion(info)
        if conclusion:
            print(f"[L1] 官方总结: {conclusion['summary'][:200]}")
            for ch in (conclusion.get('outline') or [])[:3]:
                print(f"[L1] 章节: {ch.get('title')}")
        else:
            print("[L1] 无官方 AI 总结（未登录或视频无总结）→ 降级")

        # L2a 字幕
        subtitle = await client.get_subtitle(info)
        if subtitle:
            print(f"[L2a] 字幕: {len(subtitle)} 字符，节选: {subtitle[:100]}")
        else:
            print("[L2a] 无 CC 字幕 → 降级")

        # L2b 雪碧图
        shot = await client.get_videoshot(info)
        if shot:
            urls = shot["image_urls"]
            print(f"[L2b] 雪碧图: {len(urls)} 张，网格 {shot['img_x_len']}x{shot['img_y_len']}")
            data = await client.download_image(urls[0])
            if data:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                print(f"[L2b] 首张下载 OK: {len(data)} bytes, {img.width}x{img.height}")
            else:
                print("[L2b] 首张下载失败")
        else:
            print("[L2b] 无雪碧图 → 降级")
        return 0
    except Exception as e:
        print(f"[FAIL] {type(e).__name__}: {e}")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
