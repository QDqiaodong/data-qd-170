# data-qd-170 · 电子书全文索引

已实现可离线运行的业务闭环，字段、算法约定和未支持边界详见 [数据契约](docs/contract.md)。样例均为自制合成数据，来源及许可见 fixtures/SOURCES.md。

## 独立安装

Python >=3.11（本批实测3.12.14），标准库运行与unittest测试。使用已有uv；setuptools/wheel精确锁定。每个仓库建立独立环境，不共享site-packages。

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-build.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -m unittest discover -s tests -v
```

已有公共缓存时可为两条uv pip install添加 --offline；缓存缺失时需先准备构建工具，不伪称离线成功。修改源码后重新安装。

## 端到端演示

在仓库根目录执行。run/为忽略的运行输出，演示不访问网络、不启动服务或容器。

```sh
.venv/bin/python fixtures/make_sample.py
.venv/bin/epub-index import --source fixtures/sample.epub --db run/books.sqlite
.venv/bin/epub-index search --db run/books.sqlite --query harbor
```

预期：导入2章；harbor查询按spine顺序返回 Evening、Morning。

数据库目标必须不存在，重复演示请指定另一个新数据库路径；查询是只读的。导入先完整校验到临时库，成功后原子发布，已有目标拒绝覆盖；失败退出2，旧目标保留。SQLite档案可脱离输入查询。

## 批量接收

管理员可用有序 JSON 清单一次归档一批本地 EPUB：每本书仍生成独立 SQLite 书库，整批生成可追溯的接收清单。

```sh
# manifest.json：有序数组，每项且仅含 {"source": "路径"}；
# 相对路径按清单文件所在目录解析，也接受绝对路径
.venv/bin/epub-index import-batch --manifest run/manifest.json --target run/new-arrivals
```

- 预检复用单本 import 的全部 EPUB 安全校验，读取每本书的 package identifier 与源 SHA-256。同一清单内 identifier 重复、源文件重复（同一路径或同一内容）、EPUB 无效或目标已存在都会拒绝整批，并指明清单项与原因。书籍身份只来自包内标识与内容摘要，不依据文件名。
- 全部验证与归档构建在目标同级临时目录中完成；只有整批成功才将目录原子改名为目标。任一项失败或发布失败：目标不存在、不留可见部分归档；源 EPUB 只读，不移动、不改写、不删除。目标已存在（文件、目录或符号链接）一律拒绝，不覆盖。
- 归档文件名由 identifier 净化派生（仅保留字母数字与 `-._`，截断并附 SHA-256 前缀，形如 `001-urn-demo-beta-5a899fe5ecb5.sqlite`），包内标识中的路径字符不能左右输出位置。
- 成功时目标目录含每本书一个 `.sqlite` 与一份 `receipt.json`；接收清单（同时输出到 stdout）按清单顺序记录输入项、identifier、SHA-256、标题与生成的归档文件名，并附清单自身的 SHA-256，可从每个输入追溯到实际生成的数据库。各书库与单本 import 产物格式相同，可继续使用 `versions`、`select`、`search`、`annotate`、`pending` 与作者目录命令，也可继续导入同书新版本。
- 临时目录清理失败时会报告实际残留位置，且不会宣称目标已发布。

## 多版本与读者批注

同一书籍以 EPUB package identifier 识别，版本以源文件 SHA-256 识别。向已有书库再次执行 `import` 时，新 SHA 完整校验后保存为新版本，不覆盖旧章节或搜索索引；重复导入同一 SHA 幂等返回 `unchanged`；同 SHA 的身份冲突拒绝。新版本仅在整本 EPUB 成功保存后成为当前版本，失败更新回滚，不留下可搜索的半成品。

```sh
# v1 首版（目标不存在）
.venv/bin/epub-index import --source run/v1.epub --db run/lib.sqlite
# 在 v1 第2章对精确原文摘录加批注（保存前后各至多32字符上下文）
.venv/bin/epub-index annotate --db run/lib.sqlite --chapter 2 --version 1 \
    --excerpt 'old quay' --note 'check this quay'
# 同书新版本：批注按原文摘录+上下文线索自动重定位
.venv/bin/epub-index import --source run/v2.epub --db run/lib.sqlite
.venv/bin/epub-index versions --db run/lib.sqlite
.venv/bin/epub-index search --db run/lib.sqlite --query quay --version 2
.venv/bin/epub-index select --db run/lib.sqlite --version 1
.venv/bin/epub-index pending --db run/lib.sqlite --version 2
```

- `search` 默认查当前版本，可用 `--version` 传版本 id 或完整 SHA-256；只有唯一重定位（`located`）的批注随命中章节展示，批注文字与摘录原样返回。
- 重定位有两个及以上上下文匹配候选时为 `ambiguous`，摘录消失为 `missing`；两者都不挂搜索命中，但可由 `pending` 读取，并保留原章节锚点、摘录、上下文线索和读者原文，系统不自动指派也不改写。
- 旧版本搜索继续显示该版本自己的批注位置；章节移动或路径变化不改变批注原文。
- 批注摘录必须是所选章节正文的非空连续原文子串（同章多次出现的摘录也拒绝）；FTS 分词结果、通配符和归一化空白短语不能作为锚点。
- `annotate` 的 `--excerpt/--note` 与 `--excerpt-file/--note-file` 二选一，文件内容为单个 JSON 字符串。

需要Python自带SQLite支持FTS5；实际测试覆盖真实索引、查询和非法归档拒绝。生成的样例EPUB为原创文本；不包含第三方书籍。`fixtures/make_versions.py` 构造同 identifier 的四个版本（唯一重定位、歧义、章节缺失），覆盖全部重定位状态。

## 作者目录

导入时同时保存每个版本 OPF 中 `dc:creator`/`dc:contributor` 的原始姓名、类别内声明顺序、role（缺失时为 null）与其余 OPF 属性；来源声明原样保存，不会写回规范姓名，也不会跨版本合并。

```sh
# 维护规范身份与别名（精确全名，不做模糊匹配）
.venv/bin/epub-index author-add --db run/lib.sqlite --name 'Jordan Lee'
.venv/bin/epub-index alias-add --db run/lib.sqlite --author 1 --alias 'J. Lee'
# 读取全部来源声明、未解析声明和该作者的书目
.venv/bin/epub-index contributors --db run/lib.sqlite
.venv/bin/epub-index unresolved --db run/lib.sqlite
.venv/bin/epub-index author-books --db run/lib.sqlite --author 1
# 可一次查询多个书库：首个 --db（或 --catalogue）持有目录
.venv/bin/epub-index author-books --db run/lib.sqlite --db run/other.sqlite --author 1
```

- 目录以规范身份的显示名及其显式别名作为精确匹配名；声明只有唯一命中一个身份时才解析。零命中为 `unmapped`，命中多个身份为 `ambiguous`，两种都留在 `unresolved` 中，且携带原始姓名、类别、顺序、role 和来源版本。
- 新增别名后，所有版本中匹配的来源声明进入该作者书目；撤销后重新未解析。别名冲突时其声明不归入任一身份。身份显示名可重复，不会因此自动合并。
- 书目结果按版本列出，各版本自己的来源声明不去重；切换当前版本不会抹去旧版本声明。解析在每次查询时基于当前目录计算，不会出现过期归属。
- 已有旧版归档仍可读写：首次写入目录或导入新版本时补建目录表；查询本身不修改归档。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。

## 馆刊选章与许可工作流

编辑与版权登记员共用一个本地**工作空间**（普通目录，内含独立的 `workspace.sqlite` 与 `anthologies/`），并在创建时指定一个现有归档作为**共同作者目录**。整个流程离线运行，不新增依赖；所有书库与目录都以只读方式打开，工作空间绝不改动归档库。

```sh
# 1) 用持有规范作者的现有归档建立工作空间
.venv/bin/epub-index workspace-init --workspace run/journal --catalogue run/lib.sqlite
# 2) 编辑按刊号保存有序选章计划（JSON 数组；相对路径按计划文件所在目录解析）
#    每项恰好含 archive / identifier / source_sha256 / position 四个字段
.venv/bin/epub-index plan-save --workspace run/journal --issue 1 --plan run/issue1.json
# 3) 登记员登记许可（编号自动生成、不可复用；范围与初始额度登记后不变）
.venv/bin/epub-index license-register --workspace run/journal --catalogue run/lib.sqlite \
    --author 1 --archive run/lib.sqlite --identifier urn:demo:book \
    --version-sha <完整64位SHA-256> --positions 1,2 --quota 2
# 4) 编辑申请整期预留，成功后刊发；重复刊发/取选集走幂等重放
.venv/bin/epub-index plan-reserve --workspace run/journal --issue 1 --catalogue run/lib.sqlite
.venv/bin/epub-index publish --workspace run/journal --issue 1 --catalogue run/lib.sqlite
.venv/bin/epub-index anthology --workspace run/journal --issue 1
```

**身份与授权范围。** 选章身份是三元组（package identifier、完整源 SHA-256、从 1 起的 spine 位置），`archive` 路径只用于打开文件，不能替代其中任何一项；当前版、其他版本同位置章节和搜索摘要都不能充当所选正文。同一期同一章只能出现一次。授权只看所选版本保存的 `dc:creator`：至少一条创作者声明，且每条都必须对共同目录**唯一解析**；多条声明指向同一规范作者时该章只需一份许可，`dc:contributor` 不产生授权需求。工作空间中的作者 id 一律来自指定目录自身的 `authors` 表，不会拿其他书库里数字相同的 id 来匹配。

**许可。** 一条许可指向一位规范作者、一本书的一个精确版本、一组明确允许的章节位置和一个正整数额度。同一章同一位作者可以有多条可用许可；一次额度只覆盖“一期 × 一章 × 一位作者”，另一期刊发或另一位作者都要再次扣额。不存在的作者、版本、章节或非正整数额度一律拒绝登记。`license-list` 可查看许可的原始范围、状态（`active`/`revoked`）、已用/剩余额度和全部正式使用记录。

**预留与撤回。** 保存计划不占额度；`plan-reserve` 成功才承诺整期全部章节与作者需求，并记录通过次序（`reservation_seq`）和各章解析出的作者。可行性按最大流判定：扣除正式用量、排除已撤销许可后，新计划与所有既有预留仍能一起被足额覆盖才通过；新申请失败绝不挤掉先前预留，失败原因写入计划与事件流。已预留计划必须先 `plan-withdraw` 才能修改；撤回只释放预留、保留计划，重新预留按新次序排队。`plan-list`（可带 `--issue`）展示已保存/已预留/已刊发状态、各章作者快照及 `reserved`、`reserve_failed`、`withdrawn`、`returned`、`publish_rejected`、`published` 事件与原因。

**撤销的级联处理。** `license-revoke` 只禁止该许可今后被使用，已刊使用记录照留。撤销与状态变化在同一事务内按预留通过次序生效：能与在先各期一起足额满足的继续预留；不能满足的整期退回待预留、注明撤销原因并释放全部预留；判断后续各期时不再占用已退回期的额度。只要还有其他许可足以覆盖，计划不会仅因原来“可能”用过被撤销的许可而被退回。

**刊发与选集。** 只有已预留计划能刊发。刊发前重新从归档和共同目录核对所选版本、章节和各章作者集合；缺档、声明不再唯一解析，或作者集合与预留时不同，整期拒绝、不扣额、预留保留（须明确撤回后重提）。选定正式许可时，扣除本期用量后仍须保证其他已预留各期可足额刊发，因此可能强制使用范围更窄的许可（见下例）。刊发记录、许可用量和最终 JSON 选集在同一事务提交，不可能只扣额、只存部分章节或让两个并发申请占同一份额度（工作空间写事务以 `BEGIN IMMEDIATE` 串行化）。选集按计划顺序保存每章的确切来源身份、`href`、标题与归档正文、当时的作者 id/姓名和每位作者实际使用的许可编号，不含读者私有批注。已刊内容不可改稿、不按当前目录重算；按同刊号再次 `publish` 或 `anthology` 会识别已刊记录并返回原数据——即使许可后来撤销、归档库已移走，也不重复扣额、不丢失原选集。

以规格中的甲乙两章为例：许可 A 覆盖甲、乙，许可 B 只覆盖甲，额度各一份。先预留只含甲的第一期、再预留只含乙的第二期都成功；第一期先发时必须实际使用 B，第二期才能用 A。若刊发前撤销 B，则第一期继续预留、第二期退回；一期有多章或共同作者时，任一需求得不到满足则整期无任何残余预留。这些规则与覆盖别名在预留后变得歧义、当前版与明确所选版本不同、刊发后撤销/重试/归档不可用等情形，均由 `tests/test_workspace.py` 自动化覆盖。

工作空间还提供以下命令，全部沿用“成功输出 JSON、拒绝时 stderr 给消息并退出 2”的惯例：

- `plan-withdraw --workspace DIR --issue N` — 撤回预留，计划保留。
- `license-revoke --workspace DIR --license CODE [--reason TEXT]` — 撤销并原子级联处理未刊计划，返回保留/退回的刊号。
- `anthology --workspace DIR --issue N [--output PATH]` — 取回冻结选集（可选另存一份）。

