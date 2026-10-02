"""pytest 全局配置：在导入应用前把默认数据库指向临时路径。

app.main 在模块导入时即执行 app = create_app()（沿用默认 FLOOD_DB_PATH），
测试本身虽用 tmp_path 构建独立应用，但导入期的建表仍会触碰默认库；
这里提前改写环境变量，保证测试不污染工作区的 ./data/flood.db。
"""
import os
import tempfile

os.environ.setdefault("FLOOD_DB_PATH", os.path.join(tempfile.gettempdir(), "flood-test-import.db"))
