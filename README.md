# 夜间共配值守交班与薪资

《[夜间值守与计薪交换约定](protocol.md)》记录相关业务系统已经约定的交换格式。样本是独立提供的合成输入，涉及的编号、联系代号和金额均为演练数据。每个文件可独立检查，跨文件通过格式说明中的业务引用关联。

本地使用 Python 3.11.2，资料检查使用标准库，无需安装依赖。在项目目录运行 `python3 -m unittest discover -s tests -v`。例如运行 `python3 -m json.tool --json-lines samples/shifts.ndjson` 可逐条查看一份交换资料。

## 值守、交班与薪资后端

`nightops` 包把交换资料解释成可复核的值守、交接与计薪结果，并提供可供
排班员、门店、骑手、协调员和发薪人员重复接入的追加式资料库与命令行
（`python3 -m nightops --help`）。实现说明与规则对照见
[docs/implementation.md](docs/implementation.md)。

```bash
python3 -m nightops init --store data --actor admin --role coordinator
python3 -m nightops import --store data --from-dir samples
python3 -m nightops register --store data --by admin --actor pay-1 --role payroll
python3 -m nightops rider --store data --as r-a --period 2026-11-23
python3 -m nightops payroll --store data --as pay-1 --period 2026-11-23
```

要点：

* 同一时段骑手只有一份值守承诺，不会因多个站的邀请重复计保底；
* 终端钟差只让拿不准的那一段进入待核，已分清且不相交的工时照常确认；
* 换手须双方确认、收件人有本站有效接班资格，同一段计件只记一次；
* 班薪取“已确认在岗保底”和“该班已确认计件”的较高值，跨午夜归开始营业日；
* 已签发工资与当时依据留档，更正形成独立差额并关联原签发号；
* 骑手只能看到自己的在岗时段、送件分摊和发放明细。

