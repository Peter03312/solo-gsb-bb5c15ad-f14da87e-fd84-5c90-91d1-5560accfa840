#!/usr/bin/env bash
# 山洪水位预警裁定服务 —— 端到端调用示例
# 用法：先启动服务（见 README），再执行 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8000}"
J='content-type: application/json'

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 登记监测站（上升阈值 10.0，解除阈值 8.0）"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"青石峡站","rise_threshold":10.0,"clear_threshold":8.0}' ; echo

step "2. 为监测站登记设备 DEV-001"
curl -s -X POST "$BASE/stations/1/devices" -H "$J" -d '{"device_no":"DEV-001"}'; echo

step "3. 设备补传读数（设备号 + 设备内序号 + 带时区观测时间 + 水位）"
for m in 00 05 10; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-001\",\"seq\":1$m,\"observed_at\":\"2026-10-01T08:$m:00+08:00\",\"water_level\":12.0}"; echo
done

step "4. 同号同序号同内容重试 → 返回原结果（deduplicated=true）"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":110,"observed_at":"2026-10-01T00:10:00Z","water_level":12.0}'; echo

step "5. 同号同序号异内容 → 409 冲突"
curl -s -o /dev/null -w 'HTTP %{http_code}\n' -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":110,"observed_at":"2026-10-01T08:10:00+08:00","water_level":13.0}'

step "6. 待发布裁定时间线（3 个读数达阈值、首尾 10 分钟 → 预警）"
curl -s "$BASE/stations/1/timeline"; echo

step "7. 发布：冻结公开结果"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"例行发布"}'; echo

step "8. 补传解除读数（2 条 ≤8.0，首尾 6 分钟 ≥5 → 待发布解除）"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":115,"observed_at":"2026-10-01T08:15:00+08:00","water_level":7.0}' > /dev/null
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-001","seq":121,"observed_at":"2026-10-01T08:21:00+08:00","water_level":7.0}' > /dev/null
echo "待发布状态: $(curl -s "$BASE/stations/1/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"
echo "公开状态（仍冻结为预警）: $(curl -s "$BASE/public/stations/1/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"

step "9. 审核员更正读数（带原因），裁定随之重算"
curl -s -X POST "$BASE/readings/1/correct" -H "$J" \
  -d '{"water_level":9.0,"reason":"比测后修正","auditor":"王审"}'; echo

step "10. 查询原值与历次修订"
curl -s "$BASE/readings/1/revisions"; echo

step "11. 重新发布 → 公开结果更新"
curl -s -X POST "$BASE/publish" > /dev/null
curl -s "$BASE/public/stations/1/timeline"; echo

step "12. 作废读数示例（读数 2，带原因）"
curl -s -X POST "$BASE/readings/2/void" -H "$J" \
  -d '{"reason":"传感器故障","auditor":"李审"}'; echo

# ---------------------------------------------------------------
# 阈值版本（防汛方案调整）：在新站 2 上完整演示
# ---------------------------------------------------------------

step "13. 登记新监测站（基准：上升 10.0 / 解除 8.0）与设备"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"南河坝站","rise_threshold":10.0,"clear_threshold":8.0}'; echo
curl -s -X POST "$BASE/stations/2/devices" -H "$J" -d '{"device_no":"DEV-002"}' > /dev/null

step "14. 防汛方案调整：登记阈值版本（2026-10-01T09:00+08:00 起 上升 9.0 / 解除 7.0）"
curl -s -X POST "$BASE/stations/2/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T09:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级"}'; echo

step "15. 查询阈值版本历史（baseline 为登记基准值，versions 按生效时间升序）"
curl -s "$BASE/stations/2/threshold-versions"; echo

step "16. 跨版本补读数：08:55 按基准 10（9.5 未达），09:00/09:05/09:15 按新版本 9（同刻采用新版本）"
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-002","seq":1,"observed_at":"2026-10-01T08:55:00+08:00","water_level":9.5}' > /dev/null
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-002","seq":2,"observed_at":"2026-10-01T09:00:00+08:00","water_level":9.5}' > /dev/null
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-002","seq":3,"observed_at":"2026-10-01T09:05:00+08:00","water_level":9.5}' > /dev/null
curl -s -X POST "$BASE/readings" -H "$J" \
  -d '{"device_no":"DEV-002","seq":4,"observed_at":"2026-10-01T09:15:00+08:00","water_level":9.5}' > /dev/null
curl -s "$BASE/stations/2/timeline" | python3 -c '
import sys, json
tl = json.load(sys.stdin)
print("状态:", tl["status"])
for r in tl["readings"]:
    print("  读数", r["reading_id"], r["observed_at"], "水位", r["water_level"],
          "采用上升阈值", r["rise_threshold"], "版本", r["threshold_version_id"])
print("事件:", [(e["type"], e["at"], "版本", e["threshold_version_id"]) for e in tl["events"]])
'

step "17. 阈值调整本身不产生预警/解除；重复生效时间 409、非法阈值 400、未知站 404"
curl -s -o /dev/null -w '同站重复生效时间（+00:00 表示同一瞬间）→ HTTP %{http_code}\n' \
  -X POST "$BASE/stations/2/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T01:00:00Z","rise_threshold":9.5,"clear_threshold":7.5,"reason":"重复时刻"}'
curl -s -o /dev/null -w '上升阈值不大于解除阈值 → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/2/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T10:00:00+08:00","rise_threshold":7.0,"clear_threshold":7.0,"reason":"非法"}'
curl -s -o /dev/null -w '生效时间不带时区 → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/2/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T10:00:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"无时区"}'
curl -s -o /dev/null -w '监测站不存在 → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/999/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T10:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"x"}'

step "18. 发布冻结：调整阈值只改变待发布时间线，历史快照重新发布后才更新"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"阈值版本演示发布"}' > /dev/null
echo "最新公开状态: $(curl -s "$BASE/public/current" | python3 -c 'import sys,json;print(json.load(sys.stdin)["timelines"][1]["status"])')"

# ---------------------------------------------------------------
# 防汛责任区（跨站联动）：在新站 3、4 上完整演示
# ---------------------------------------------------------------

step "19. 登记两个成员站与设备（青石峡片区）"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"西坡站","rise_threshold":10.0,"clear_threshold":8.0}' > /dev/null
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"东沟站","rise_threshold":10.0,"clear_threshold":8.0}' > /dev/null
curl -s -X POST "$BASE/stations/3/devices" -H "$J" -d '{"device_no":"DEV-003"}' > /dev/null
curl -s -X POST "$BASE/stations/4/devices" -H "$J" -d '{"device_no":"DEV-004"}' > /dev/null

step "20. 两站同时刻各补传 3 条达阈值读数（2026-10-01 10:00/10:05/10:10）"
for D in DEV-003 DEV-004; do
  for m in 00 05 10; do
    curl -s -X POST "$BASE/readings" -H "$J" \
      -d "{\"device_no\":\"$D\",\"seq\":30$m,\"observed_at\":\"2026-10-01T10:$m:00+08:00\",\"water_level\":12.0}" > /dev/null
  done
done

step "21. 登记责任区（非空名称 + 带时区生效时间 10:00 + 成员 [3,4] + K=2）"
curl -s -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"青石峡片区","effective_at":"2026-10-01T10:00:00+08:00","station_ids":[3,4],"trigger_count":2}'
echo

step "22. 待发布区域联动：同刻合并后两站齐预警 → 10:10 区域预警（每刻最多一个事件）"
curl -s "$BASE/zones/1/timeline" | python3 -c '
import sys, json
z = json.load(sys.stdin)
print("区域状态:", z["status"], "| 当前预警站:", z["warning_station_ids"])
for e in z["events"]:
    print("  事件:", e["type"], e["at"], "发生时预警站:", e["warning_station_ids"])
'

step "23. 责任区登记校验：未知站 404、重复成员/无时区/K 越界 400"
curl -s -o /dev/null -w '未知监测站 ID      → HTTP %{http_code}\n' -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"x","effective_at":"2026-10-01T10:00:00+08:00","station_ids":[3,999],"trigger_count":2}'
curl -s -o /dev/null -w '重复成员           → HTTP %{http_code}\n' -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"x","effective_at":"2026-10-01T10:00:00+08:00","station_ids":[3,3],"trigger_count":1}'
curl -s -o /dev/null -w '生效时间不带时区   → HTTP %{http_code}\n' -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"x","effective_at":"2026-10-01T10:00:00","station_ids":[3],"trigger_count":1}'
curl -s -o /dev/null -w 'K=2 但成员仅 1 个  → HTTP %{http_code}\n' -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"x","effective_at":"2026-10-01T10:00:00+08:00","station_ids":[3],"trigger_count":2}'
curl -s -o /dev/null -w '空白名称           → HTTP %{http_code}\n' -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"   ","effective_at":"2026-10-01T10:00:00+08:00","station_ids":[3],"trigger_count":1}'

step "24. 审核作废一站关键读数 → 待发布区域立即重算为正常"
VOID_ID="$(curl -s "$BASE/stations/4/readings" | python3 -c 'import sys,json;print(json.load(sys.stdin)[-1]["id"])')"
curl -s -X POST "$BASE/readings/$VOID_ID/void" -H "$J" -d '{"reason":"误报剔除"}' > /dev/null
echo "待发布区域状态: $(curl -s "$BASE/zones/1/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')"

step "25. 尚无该责任区发布 → 公开查询明确报无结果（404）"
curl -s -w ' [HTTP %{http_code}]\n' "$BASE/public/zones/1/timeline"

step "26. 发布：与各站时间线一同冻结（响应含 zones 摘要）"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"责任区演示发布"}' \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("发布号:",d["publication_id"],"| zones:",d["zones"])'

step "27. 生效前已预警计入初始状态：新站 5 在 10:00 前预警，责任区 11:00 生效即预警"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"北坳站","rise_threshold":10.0,"clear_threshold":8.0}' > /dev/null
curl -s -X POST "$BASE/stations/5/devices" -H "$J" -d '{"device_no":"DEV-005"}' > /dev/null
for m in 00 05 10; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-005\",\"seq\":50$m,\"observed_at\":\"2026-10-01T10:$m:00+08:00\",\"water_level\":12.0}" > /dev/null
done
curl -s -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"北坳片区","effective_at":"2026-10-01T11:00:00+08:00","station_ids":[5],"trigger_count":1}' \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
p = d["pending_zone"]
print("初始满足 K → 区域状态:", p["status"], "| 事件:", [(e["type"], e["at"]) for e in p["events"]])
'

step "28. 重新发布后公开结果含两个责任区；历史快照保持不变"
curl -s -X POST "$BASE/publish" -H "$J" -d '{"note":"含北坳片区"}' > /dev/null
curl -s "$BASE/public/current" | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("最新发布号:", d["publication_id"])
for z in d["zone_timelines"]:
    print("  责任区", z["zone_id"], z["zone_name"], "状态:", z["status"],
          "事件:", [(e["type"], e["at"], e["warning_station_ids"]) for e in z["events"]])
'
echo "历史发布 1 的区域数: $(curl -s "$BASE/publications/1" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["zone_timelines"]))')"

# ---------------------------------------------------------------
# 阈值版本预演（登记前不落库演练）：在新站 6、7 与责任区 3 上完整演示
# ---------------------------------------------------------------

step "29. 预演准备：登记两个站（基准 10/8）与设备，并各补 3 条读数"
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"南坳站","rise_threshold":10.0,"clear_threshold":8.0}' > /dev/null
curl -s -X POST "$BASE/stations" -H "$J" \
  -d '{"name":"北岭站","rise_threshold":10.0,"clear_threshold":8.0}' > /dev/null
curl -s -X POST "$BASE/stations/6/devices" -H "$J" -d '{"device_no":"DEV-006"}' > /dev/null
curl -s -X POST "$BASE/stations/7/devices" -H "$J" -d '{"device_no":"DEV-007"}' > /dev/null
# 南坳 9.5：基准 10 下不预警；北岭 12.0：基准 10 下预警
for m in 00 05 10; do
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-006\",\"seq\":60$m,\"observed_at\":\"2026-10-01T11:$m:00+08:00\",\"water_level\":9.5}" > /dev/null
  curl -s -X POST "$BASE/readings" -H "$J" \
    -d "{\"device_no\":\"DEV-007\",\"seq\":70$m,\"observed_at\":\"2026-10-01T11:$m:00+08:00\",\"water_level\":12.0}" > /dev/null
done
curl -s -X POST "$BASE/zones" -H "$J" \
  -d '{"name":"南岭片区","effective_at":"2026-10-01T11:00:00+08:00","station_ids":[6,7],"trigger_count":2}' > /dev/null
echo "预演前：南坳状态 $(curl -s "$BASE/stations/6/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')，区域 3 状态 $(curl -s "$BASE/zones/3/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')（仅北岭预警，1 < K=2）"

step "30. 预演：南坳站拟自 2026-10-01T11:00+08:00 起上升阈值降到 9（不落库，返回本站与区域 3 的现状/预演）"
curl -s -X POST "$BASE/stations/6/threshold-versions/preview" -H "$J" \
  -d '{"effective_at":"2026-10-01T11:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级预演"}' \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("dry_run:", d["dry_run"], "| 候选临时版本 id:", d["candidate"]["threshold_version_id"])
print("南坳 现状:", d["current"]["status"], "→ 预演:", d["preview"]["status"],
      "| 预演事件:", [(e["type"], e["at"], "版本", e["threshold_version_id"]) for e in d["preview"]["events"]])
z = d["zones"][0]
print("责任区", z["zone_id"], z["zone_name"], "现状:", z["current"]["status"],
      z["current"]["warning_station_ids"], "→ 预演:", z["preview"]["status"],
      z["preview"]["warning_station_ids"])
print("区域预演事件:", [(e["type"], e["at"], e["warning_station_ids"]) for e in z["preview"]["events"]])
'

step "31. 预演不落库：版本历史无候选、待发布查询与已冻结发布均不变"
echo "南坳版本数: $(curl -s "$BASE/stations/6/threshold-versions" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["versions"]))')（应为 0）"
echo "待发布南坳仍为: $(curl -s "$BASE/stations/6/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')（normal）"
echo "待发布区域 3 仍为: $(curl -s "$BASE/zones/3/timeline" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')（normal）"

step "32. 预演沿用登记校验：未知站 404、无时区/非法阈值/空原因 400"
curl -s -o /dev/null -w '未知站             → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/999/threshold-versions/preview" -H "$J" \
  -d '{"effective_at":"2026-10-01T11:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"x"}'
curl -s -o /dev/null -w '生效时间不带时区   → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/6/threshold-versions/preview" -H "$J" \
  -d '{"effective_at":"2026-10-01T11:00:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"x"}'
curl -s -o /dev/null -w '上升不大于解除     → HTTP %{http_code}\n' \
  -X POST "$BASE/stations/6/threshold-versions/preview" -H "$J" \
  -d '{"effective_at":"2026-10-01T11:00:00+08:00","rise_threshold":7.0,"clear_threshold":7.0,"reason":"x"}'

step "33. 预演同刻冲突：先正式登记一版 12:00，再以同一瞬间（Z 表示）预演 → 409"
curl -s -X POST "$BASE/stations/6/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T12:00:00+08:00","rise_threshold":9.5,"clear_threshold":7.5,"reason":"正式登记"}' > /dev/null
curl -s -w ' [HTTP %{http_code}]\n' -X POST "$BASE/stations/6/threshold-versions/preview" -H "$J" \
  -d '{"effective_at":"2026-10-01T04:00:00Z","rise_threshold":9.2,"clear_threshold":7.2,"reason":"同刻预演"}'

step "34. 正式登记预演的调整（11:00 起 9/7）：无其他写入时结果与预演一致"
curl -s -X POST "$BASE/stations/6/threshold-versions" -H "$J" \
  -d '{"effective_at":"2026-10-01T11:00:00+08:00","rise_threshold":9.0,"clear_threshold":7.0,"reason":"入汛提级"}' \
  | python3 -c '
import sys, json
d = json.load(sys.stdin)
p = d["pending_adjudication"]
print("登记后南坳:", p["status"], "| 事件:", [(e["type"], e["at"], "版本", e["threshold_version_id"]) for e in p["events"]],
      "（与步骤 30 的预演 status/类型/时刻一致，版本 id 换为真实 id）")
'
echo "登记后区域 3: $(curl -s "$BASE/zones/3/timeline" \
  | python3 -c 'import sys,json;z=json.load(sys.stdin);print(z["status"], [(e["type"],e["at"]) for e in z["events"]])')"
echo "（状态/事件类型/时刻与步骤 30 预演一致；预演的临时版本 id -1 已换为正式版本 id）"

echo
echo "演示完成。交互式文档：$BASE/docs"
