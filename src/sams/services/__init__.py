"""业务编排层：把 core 契约、datasource 适配器、storage 仓库串成用例。

依赖方向（唯一允许的方向）：cli → services → {datasource, storage} → core。
本层的类全部通过构造函数拿依赖，因此可以在不联网、不建库的情况下被测试。
"""
