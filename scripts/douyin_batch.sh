#!/usr/bin/env bash
# douyin_batch.sh — 并行下载多条抖音作品，各自输出 JSON 到 stdout。
#
# 用法:
#   bash scripts/douyin_batch.sh <输出根目录> <URL1> [URL2 ...]
#
# 每个作品下载到 <输出根目录>/<tag>/ 下的 _tmp_douyin_dl.mp4，
# 同时把元数据 JSON 写到 <输出根目录>/<tag>/meta.json，
# 并在 stdout 打印一行一条的汇总（tag<TAB>json）。下载完成后再统一 mv 成规范名。
#
# 为什么并行：每条都要开一次 playwright chromium（~15s），串行等不起。
# 为什么各用各的目录：douyin_fetch.py 的临时文件名固定（_tmp_douyin_dl.mp4），
# 共用目录会互相覆盖。

set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FETCH="$SCRIPT_DIR/douyin_fetch.py"

OUT_ROOT="${1:-}"
shift
if [[ -z "$OUT_ROOT" || $# -eq 0 ]]; then
  echo "Usage: $0 <输出根目录> <URL1> [URL2 ...]" >&2
  exit 1
fi

mkdir -p "$OUT_ROOT"

i=0
pids=()
for url in "$@"; do
  i=$((i + 1))
  tag=$(printf "v%02d" "$i")
  dir="$OUT_ROOT/$tag"
  mkdir -p "$dir"
  (
    python3 "$FETCH" "$url" "$dir" --json > "$dir/meta.json" 2> "$dir/fetch.err"
    echo "$?" > "$dir/exit"
  ) &
  pids+=($!)
  echo "$tag $url" >> "$OUT_ROOT/manifest.tsv"
done

for p in "${pids[@]}"; do
  wait "$p"
done

echo "=== 下载完成，落库前请把每个 <tag>/_tmp_douyin_dl.mp4 mv 成规范名 ===" >&2
for d in "$OUT_ROOT"/v*; do
  [[ -d "$d" ]] || continue
  tag="$(basename "$d")"
  code="$(cat "$d/exit" 2>/dev/null || echo "?")"
  if [[ "$code" != "0" ]]; then
    echo "$tag	FAILED(exit=$code)	$(tail -3 "$d/fetch.err" 2>/dev/null | tr '\n' ' ')" >&2
    continue
  fi
  echo "$tag	$(cat "$d/meta.json" | tr -d '\n')"
done
