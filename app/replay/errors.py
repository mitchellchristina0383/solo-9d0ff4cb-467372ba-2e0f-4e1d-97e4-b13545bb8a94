"""回放引擎的自定义异常。"""


class ReplayError(ValueError):
    """操作记录解析或回放参数非法。"""
