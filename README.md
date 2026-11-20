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

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。
