#!/usr/bin/env bash
# 產生測試影片：5 個畫面（模擬 5 個操作頁面），每個 3 秒；
# 畫面中有一個持續移動的小黑塊模擬滑鼠游標（不應被當成畫面切換）。
# 用法：bash make_test_video.sh [輸出檔，預設 test_video.mp4]
set -euo pipefail
OUT="${1:-test_video.mp4}"
FONT="C\\:/Windows/Fonts/msjh.ttc"
PAGES=("0xF4F4F4:登入頁" "0xFFE0CC:首頁儀表板" "0xDDE8F0:新增訂單表單" "0xFAE0B8:確認視窗" "0xE0F0E0:完成畫面")
N=${#PAGES[@]}

inputs=()
filters="color=c=black:s=12x18:r=25:d=3,split=$N"
for ((i = 0; i < N; i++)); do filters+="[c$i]"; done
filters+=";"
labels=""
for ((i = 0; i < N; i++)); do
  color="${PAGES[$i]%%:*}"; text="${PAGES[$i]#*:}"
  inputs+=(-f lavfi -i "color=c=${color}:s=1280x720:d=3:r=25")
  filters+="[$i:v]drawbox=x=0:y=0:w=1280:h=64:color=0x1A1A1A:t=fill,"
  filters+="drawtext=fontfile='${FONT}':text='示範系統':x=24:y=16:fontsize=30:fontcolor=white,"
  filters+="drawtext=fontfile='${FONT}':text='步驟 $((i + 1))　${text}':x=(w-tw)/2:y=(h-th)/2:fontsize=56:fontcolor=0x1A1A1A,"
  filters+="drawbox=x=1000:y=560:w=200:h=60:color=0xFF5000:t=fill[bg$i];"
  filters+="[bg$i][c$i]overlay=x='300+t*60':y=480:eval=frame[v$i];"
  labels+="[v$i]"
done
filters+="${labels}concat=n=$N:v=1:a=0,format=yuv420p[out]"
ffmpeg -hide_banner -loglevel error -y "${inputs[@]}" -filter_complex "$filters" -map "[out]" "$OUT"
echo "已產生 $OUT"
