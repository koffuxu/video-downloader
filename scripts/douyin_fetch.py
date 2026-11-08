#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""douyin_fetch.py — 抖音作品下载（兼容 MSE blob 流播放器 + 多分辨率选档）

为什么需要 v2：抖音 web 播放器改用 MSE（Media Source Extensions）流式播放，
`video.currentSrc` 拿到的是 `blob:https://www.douyin.com/...`，不是 CDN 直链，
拿它去 curl 会报 `URL rejected: Port number was not a decimal number`。

v2 的做法：拦截 `aweme/detail` XHR 与播放器发出的 CDN 请求，拿到
`https://www.douyin.com/aweme/v1/play/?video_id=...&file_id=...` 这类
**渐进式直链**（音视频合一），逐个探测分辨率后选最高清的一档下载。
带 `Referer: https://www.douyin.com/` 即可，不需要登录态。

选档铁律：**按分辨率选，不按文件体积选**。体积受码率影响，与分辨率无关；
带水印档（URL 含 `watermark=1`）常常比无水印高清档还大，按体积选会选错。

用法:
    python3 scripts/douyin_fetch.py "<抖音链接或短链>" [输出目录] [--json]
    python3 scripts/douyin_fetch.py "<URL>" [输出目录] --list   # 只列档位不下载
    python3 scripts/douyin_fetch.py "<URL>" [输出目录] --no-watermark

输出: stdout 只打印最终 JSON（--json）或摘要，**进度信息一律走 stderr**，
这样 stdout 可以直接管道给 json.loads。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

UA_PC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
REFERER = "https://www.douyin.com/"

PLAY_RE = re.compile(r"https://www\.douyin\.com/aweme/v1/play/\?")


# ---------------------------------------------------------------- helpers
def log(msg):
    """进度信息走 stderr，保证 stdout 只有 JSON。"""
    print(msg, file=sys.stderr, flush=True)


def resolve_final_url(url):
    """短链 v.douyin.com/xxx 会 302 到真实分享页，取最终 URL。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA_PC})
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
    with opener.open(req, timeout=30) as resp:
        return resp.geturl()


def extract_aweme_id(url):
    m = re.search(r"/(?:share/)?video/(\d{15,25})", url)
    if m:
        return m.group(1)
    m = re.search(r"modal_id=(\d+)", url)
    return m.group(1) if m else ""


def clean_title(desc):
    """剥掉 hashtag、@提及 与平台后缀，只留作品名。

    desc 里全是 hashtag / @提及 时（例如 `#话题 @DOU+小助手`），剥完就没有
    可用名字了——此时第一个 hashtag 往往就是舞名，留给上层决定怎么处理。
    """
    t = re.sub(r"#[^\s#]+", " ", desc or "")
    t = re.sub(r"@[^\s@]+", " ", t)
    t = re.sub(r"\s*[-–—]\s*(抖音|快手|Bilibili|小红书)\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—|")
    return t


def first_hashtag(desc):
    m = re.search(r"#([^\s#]+)", desc or "")
    return m.group(1) if m else ""


def _collect_play_urls(item):
    """从 aweme_detail 里收集所有渐进式 play 直链（音视频合一）。"""
    out = []
    vid = item.get("video") or {}
    for key in ("play_addr", "play_addr_h264", "play_addr_265", "download_addr"):
        v = vid.get(key)
        if isinstance(v, dict):
            out += v.get("url_list") or []
    for br in vid.get("bit_rate") or []:
        pa = br.get("play_addr") or {}
        out += pa.get("url_list") or []
    return out


def head_size(url):
    """HEAD 拿 Content-Length 与 content-type，失败返回 (0, '')。"""
    try:
        out = subprocess.run(
            ["curl", "-sIL", "-A", UA_MOBILE, "-e", REFERER, "--max-time", "15", url],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return 0, ""
    size, ctype = 0, ""
    for line in out.splitlines():
        low = line.lower()
        if low.startswith("content-length:"):
            try:
                size = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
        elif low.startswith("content-type:"):
            ctype = line.split(":", 1)[1].strip()
    return size, ctype


def is_watermarked(url):
    """URL 带 watermark=1 的档位是带水印版本。"""
    return "watermark=1" in url


# ---------------------------------------------------------------- browser
def fetch_page(aweme_id):
    """打开作品页，拦截 aweme/detail 与 CDN 请求。返回 (item, play_urls)。"""
    from playwright.sync_api import sync_playwright

    chunks = []
    cdn = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(user_agent=UA_PC, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        def on_response(resp):
            u = resp.url
            if "aweme/detail" in u or "web/api/v2/aweme" in u:
                try:
                    chunks.append(resp.text())
                except Exception:
                    pass
            if "douyinvod" in u or "zjcdn" in u or "/aweme/v1/play" in u:
                cdn.append(u)

        page.on("response", on_response)
        try:
            page.goto("https://www.douyin.com/video/%s" % aweme_id,
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            # 触发播放，逼播放器去拉 CDN 分片
            try:
                page.evaluate("""() => {
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; v.play(); }
                }""")
            except Exception:
                pass
            page.wait_for_timeout(6000)
        finally:
            browser.close()

    item = None
    for raw in chunks:
        try:
            d = json.loads(raw)
        except Exception:
            continue
        it = d.get("aweme_detail") or (d.get("aweme_list") or [{}])[0]
        if it and it.get("create_time"):
            item = it
            break

    urls = []
    if item:
        urls += _collect_play_urls(item)
    urls += cdn

    # 只保留渐进式 play 直链（音视频合一），排除 dash 分片和 blob
    progressive = []
    seen = set()
    for u in urls:
        if not u or u.startswith("blob:"):
            continue
        if PLAY_RE.search(u) and "/dash/" in u:
            continue
        if PLAY_RE.search(u) and u not in seen:
            seen.add(u)
            progressive.append(u)
    return item, progressive


# ---------------------------------------------------------------- rendition
def probe_url(url, workdir):
    """下载候选到临时文件并 ffprobe 出分辨率。返回 dict 或 None。"""
    os.makedirs(workdir, exist_ok=True)
    tmp = os.path.join(workdir, "_probe_cand.mp4")
    try:
        subprocess.run(
            ["curl", "-sL", "-A", UA_MOBILE, "-e", REFERER, "--max-time", "180",
             "-o", tmp, url], check=False)
        if not (os.path.exists(tmp) and os.path.getsize(tmp) > 10000):
            return None
        meta = probe(tmp)
        vs = [s for s in (meta.get("streams") or []) if s.get("codec_type") == "video"]
        if not vs:
            return None
        v = vs[0]
        return {
            "width": v.get("width") or 0,
            "height": v.get("height") or 0,
            "vcodec": v.get("codec_name") or "",
            "size": os.path.getsize(tmp),
        }
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def choose_rendition(urls, workdir, no_watermark=False):
    """逐个候选探测分辨率，返回 (最佳, 全部候选)。

    排序键：(像素数, 非水印优先, 体积)。**不按体积选最高清**——体积随码率变，
    带水印的 720p 常常比无水印的 1080p 还大。
    """
    cands = []
    for u in urls:
        s, c = head_size(u)
        if s > 0:
            cands.append({"url": u, "head_size": s, "ctype": c,
                          "watermark": is_watermarked(u)})
    if not cands:
        return None, []
    if no_watermark:
        keep = [c for c in cands if not c["watermark"]]
        if keep:
            cands = keep

    for c in cands:
        info = probe_url(c["url"], workdir)
        if info:
            c.update(info)
            c["pixels"] = c["width"] * c["height"]

    probed = [c for c in cands if "pixels" in c]
    if not probed:
        log(">>> 全部候选探测失败，退回按体积选最大的")
        cands.sort(key=lambda c: c["head_size"], reverse=True)
        return cands[0], cands

    probed.sort(key=lambda c: (c["pixels"], not c["watermark"], c["head_size"]),
                reverse=True)
    return probed[0], probed


def download(url, out_path):
    cmd = ["curl", "-L", "-A", UA_MOBILE, "-e", REFERER,
           "--retry", "3", "--retry-delay", "2", "-o", out_path, url]
    subprocess.run(cmd, check=False)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 100000


def probe(path):
    """ffprobe 读规格，失败返回空 dict。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration,size:stream=codec_name,codec_type,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60).stdout
        return json.loads(out)
    except Exception:
        return {}


def time_str(ts):
    import datetime
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="下载抖音作品（兼容新版 MSE 播放器）")
    ap.add_argument("url", help="抖音链接（短链 v.douyin.com 或长链均可）")
    ap.add_argument("out_dir", nargs="?", default=os.path.expanduser("~/Downloads"))
    ap.add_argument("--json", action="store_true", help="stdout 输出 JSON")
    ap.add_argument("--list", action="store_true", help="只列候选档位，不下载")
    ap.add_argument("--no-watermark", action="store_true", help="排除带水印档位")
    args = ap.parse_args()

    try:
        final_url = resolve_final_url(args.url)
    except Exception as e:
        log("ERROR: 链接解析失败 %r" % (e,))
        return 2

    aweme_id = extract_aweme_id(final_url)
    if not aweme_id:
        log("ERROR: 无法从链接解析 aweme_id: %s" % final_url)
        return 2

    item, urls = fetch_page(aweme_id)
    if not item:
        log("ERROR: 没抓到 aweme/detail 响应（页面改版或被风控）")
        return 3
    if not urls:
        log("ERROR: 没抓到可用的 play 直链")
        return 3

    workdir = os.path.join(args.out_dir, "_probe")
    best, cands = choose_rendition(urls, workdir, args.no_watermark)
    if not best:
        log("ERROR: 所有候选直链 HEAD 都失败")
        return 4

    for c in cands:
        log("    %sx%s %-10s %8.1fMB %s" % (
            c.get("width", "?"), c.get("height", "?"), c.get("vcodec", "?"),
            c.get("size", c["head_size"]) / 1048576.0,
            "watermarked" if c["watermark"] else ""))

    if args.list:
        print(json.dumps({"aweme_id": aweme_id, "candidates": cands},
                         ensure_ascii=False, indent=2))
        return 0

    os.makedirs(args.out_dir, exist_ok=True)
    tmp = os.path.join(args.out_dir, "_tmp_douyin_dl.mp4")
    log(">>> 下载 %sx%s (%.1f MB) %s" % (
        best.get("width", "?"), best.get("height", "?"),
        best.get("size", best["head_size"]) / 1048576.0,
        "watermarked" if best["watermark"] else ""))
    if not download(best["url"], tmp):
        log("ERROR: 下载失败")
        return 4

    meta = probe(tmp)
    vstreams = [s for s in (meta.get("streams") or []) if s.get("codec_type") == "video"]
    astreams = [s for s in (meta.get("streams") or []) if s.get("codec_type") == "audio"]
    v = vstreams[0] if vstreams else {}
    a = astreams[0] if astreams else {}

    desc = item.get("desc") or ""
    out = {
        "aweme_id": aweme_id,
        "author": (item.get("author") or {}).get("nickname") or "",
        "title": clean_title(desc),
        "first_hashtag": first_hashtag(desc),
        "desc": desc,
        "publish_datetime": time_str(item.get("create_time")),
        "duration_sec": round((item.get("duration") or 0) / 1000.0, 1),
        "file": tmp,
        "size": os.path.getsize(tmp),
        "width": v.get("width"),
        "height": v.get("height"),
        "video_codec": v.get("codec_name"),
        "audio_codec": a.get("codec_name"),
        "watermarked": best["watermark"],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for k, val in out.items():
            print("%-16s %s" % (k, val))
    return 0


if __name__ == "__main__":
    sys.exit(main())
