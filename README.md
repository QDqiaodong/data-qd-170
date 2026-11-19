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

需要Python自带SQLite支持FTS5；实际测试覆盖真实索引、查询和非法归档拒绝。生成的样例EPUB为原创两章文本；不包含第三方书籍。

单操作者本地批处理；不承诺并发发布或断电恢复。代码/样例采用MIT许可。
