# 夜间共配值守交班与薪资 —— 实现说明

本目录在既有交换约定（[protocol.md](protocol.md)）之上，用标准库实现了一套
可重复接入的后端：把值守、交接、计件与薪资资料解释成**可复核的事实**，
并给排班员、门店、骑手、协调员和发薪人员各自划出查询边界。

## 快速开始

```bash
# 建立资料库并导入样本交换资料
python3 -m nightops init --store data --actor admin --role coordinator
python3 -m nightops import --store data --from-dir samples

# 登记各角色
python3 -m nightops register --store data --by admin --actor sched-1 --role scheduler
python3 -m nightops register --store data --by admin --actor pay-1   --role payroll
python3 -m nightops register --store data --by admin --actor r-a     --role rider
python3 -m nightops register --store data --by admin --actor shop-pine --role shop --shop store-pine
python3 -m nightops register --store data --by admin --actor coord-1 --role coordinator

# 查询
python3 -m nightops schedule --store data --as sched-1          # 排班与承诺
python3 -m nightops shop     --store data --as shop-pine        # 本店包裹与待交班
python3 -m nightops rider    --store data --as r-a --period 2026-11-23
python3 -m nightops payroll  --store data --as pay-1 --period 2026-11-23

# 签发（重复签发同一期不会改写原记录，只报告跳过）
python3 -m nightops issue --store data --as pay-1 --period 2026-11-23
```

编程接口：

```python
from nightops.exchange import load_exchange_dir
from nightops.ledger import build_ledger
from nightops.payroll import PayrollEngine

ledger = build_ledger(load_exchange_dir("samples"))
engine = PayrollEngine(ledger)
engine.shift_pay("nite-a").describe()
engine.statement("r-a", __import__("datetime").date(2026, 11, 23)).describe()
```

## 规则如何落地

### 值守与承诺

* 每个骑手在同一时段只有**一份**值守承诺。重叠的邀请按接受先后（未给出
  接受时刻时按班次开始时刻）先到先得；后到的邀请只取得尚未被覆盖的区间，
  冲突写入 `commitments.overlap_rejections`（`nightops/shifts.py`）。
* 同一人重叠的两个站不会因此得到两份保底；没有取得承诺的班标记为
  `unassigned`，不计保底，也不把它“可能的保底”当作待发款。

### 在岗证据与终端钟差

* `attendance` 的 `[earliest, latest]` 是设备允许的实际时刻范围。签到与
  签退都有时，**一定在岗**的区间是 `[签到最晚, 签退最早]`；其余部分进入
  `pending`（待核），不猜测（`nightops/ledger.py::_compute_attendance`）。
* 缺签到/缺签退、证据矛盾、签到时段交叠、同人不同站时，只暂停**受影响
  的区间**；已能分清且不相交的工时照常确认。
* 签到范围跨过交班点时，接班人可能有迟到空档：该段记入原骑手的
  `pending`，并由原骑手确认是否续守（见下）。

### 续守

* 原骑手同意继续值守时，交接空档归**原骑手**，接班班不再计这段——同一
  承诺只有一位责任人。续守确认以 `continuations` 文件（或服务接口）提供，
  双方确认后生效（`nightops/ledger.py::_apply_continuations`）。

### 交接与计件

* `switch` 必须同次 `from` 和 `to` 双方确认；只有一方确认时实物不移动，
  单号显示为 `pending_handoff`，整单计件待核。
* 收件人须在该站有覆盖交接时刻的有效承诺；否则换手不成立，实物回到
  原骑手（`nightops/ledger.py::_pieces_for`）。
* 计件只对已交付的单确认。无换手时揽收骑手得全程 `direct`；有换手时按
  `pickup` / `relay` / `delivery` 分给最初揽收人、每次收件的换手骑手、
  最终送达者，**同一段只记一次**。
* 计件归“骑手承接此单时”的有效承诺班：跨班尾段仍归承接班，不按送达
  时刻改派。
* 退回门店的未送达件不发计件。

### 计薪

* 每个班分别比较：`已确认在岗分钟折出的保底` 与 `该班已确认计件之和`，
  取高者为班薪；**不是**先加总再取高。
* 跨午夜的班归开始营业日；双周工资期以周一为起点（默认锚点
  2026-01-05，可配置）。
* 未确认的工时/计件不计入实发，列为 `pending_items` 并说明原因；其合计
  标为上限，实际以核实后的归属为准。
* 申诉核实后，协调员只更正争议范围；更正记录带申诉号、协调员与理由。
* 已签发工资保存当时的依据（`basis_digest` 与逐班明细）；更正形成**独立
  差额**，关联原签发号，并排到下一期。未签发范围按新事实重算。

### 单号查询

`parcel_status` 用与计件相同的判断回答三种状态：

* `with_original` —— 仍在原骑手手里；
* `switched` —— 已换手（可继续显示仍待确认的换手）；
* `pending_handoff` —— 换手确认不完整，待交班。

## 角色边界

| 角色 | 能看 | 不能做 |
| --- | --- | --- |
| 排班员 scheduler | 班次、承诺、在岗证据、交班空档 | 看金额、签发、改已到岗的班 |
| 门店 shop | 本店包裹、交接、当前持件人 | 看个人工资 |
| 骑手 rider | 自己的在岗时段、送件分摊、发放明细 | 看他人记录 |
| 发薪人员 payroll | 工资单、已签发记录 | 修正更（须协调员） |
| 协调员 coordinator | 全部 | — |

所有写入追加到资料库（`nightops/store.py`），快照由重放得到；任何终端
重新接入都看到同一组事实，并有 `events` 审计流水。

## 资料库文件

除交换约定的五份资料外，资料库还保存：

| 文件 | 用途 |
| --- | --- |
| `acceptances.ndjson` | 骑手接受/拒绝邀请 |
| `continuations.ndjson` | 续守的双方确认 |
| `corrections.ndjson` | 协调员更正（在岗区间、换手时刻、补记送达） |
| `adjustments.ndjson` | 已签发工资的独立差额 |
| `appeals.ndjson` | 骑手申诉及处理结果 |
| `issuances.ndjson` | 已签发工资（含当时依据） |
| `actors.ndjson` | 角色登记 |
| `events.ndjson` | 审计流水 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_exchange.py` 校验交换样本自身的编号与时序；
`tests/test_nightops.py` 覆盖承诺唯一性、钟差、改班生效、换手双方确认、
计件分摊、跨班尾段、保底取高、跨午夜归属、待核款、签发不可改写、差额
独立、申诉只改争议范围、角色隔离与多终端一致性。
