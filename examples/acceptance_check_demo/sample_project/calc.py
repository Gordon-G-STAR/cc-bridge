"""合同示例:一个小计算模块(故意留一处边界缺陷,给 agent 语义项发现)。"""


def add(a, b):
    return a + b


def divide(a, b):
    # 故意没有对 b == 0 做处理 —— 用来演示「agent 语义项」能发现"边界未处理"。
    return a / b
