"""code_sandbox 语句级 AST 白名单测试（DESIGN 6.5 / D8 修复的验收）。

两类用例：逃逸载荷必须抛 UnsafeCode；合法数据处理代码必须原样可运行。
"""

import pytest

from code_sandbox import UnsafeCode, safe_exec
from agent import ToolRegistry


# ---------------------------------------------------------------------------
# 逃逸载荷：必须被拒绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        # 经典 __class__/__mro__ 链（修复前实测可逃逸的 PoC 原型）
        "x = ().__class__.__mro__[1].__subclasses__()",
        # 完整 PoC：走到 os 模块
        (
            "wrap = [c for c in ().__class__.__mro__[1].__subclasses__()"
            ' if c.__name__ == "_wrap_close"][0]\n'
            'getcwd = wrap.__init__.__globals__["getcwd"]\n'
            "result = getcwd()"
        ),
        # 字符串对象上的属性/方法
        'x = "".join(["a"])',
        # math 的 dunder 属性（模块对象也是逃逸跳板）
        "x = math.__class__",
        # import 语句
        "import os",
        "from os import system",
        # 函数/类定义与 lambda（可携带任意体的作用域）
        "def f():\n    return 1",
        "f = lambda: 1",
        "class C:\n    pass",
        # 推导式
        "x = [i for i in range(3)]",
        # try/with
        "try:\n    pass\nexcept Exception:\n    pass",
        "with open('x') as f:\n    pass",
        # 非白名单调用（含 getattr / open / eval）
        "x = getattr(1, 'real')",
        "f = open('secret')",
        "x = eval('1+1')",
        "x = __import__('os')",
        # **kwargs 形态
        "print(**{})",
    ],
)
def test_escape_payloads_rejected(payload):
    with pytest.raises(UnsafeCode):
        safe_exec(payload)


def test_math_dunder_attribute_rejected():
    # math 模块对象本身也是逃逸跳板：dunder 属性必须拒绝
    with pytest.raises(UnsafeCode):
        safe_exec("x = math.__class__")


# ---------------------------------------------------------------------------
# 合法代码：必须原样可运行
# ---------------------------------------------------------------------------
def test_tool_description_example_runs():
    # agent.py 工具描述里给模型的示例代码，逐字使用
    code = (
        "amounts = [2500000, 2278481, 2541806, 2282609, 2388060]\n"
        "total = sum(amounts)\n"
        'print(f"Total: ${total:,.2f}")'
    )
    namespace, output = safe_exec(code)
    assert namespace["total"] == 11990956
    assert output == "Total: $11,990,956.00\n"


def test_loops_dicts_math_and_membership():
    # 注意：沙箱禁止变量上的方法调用（converted.values() 会被拒），
    # 求和须用显式循环——这正是白名单的预期约束。
    code = (
        "rates = {'EUR': 0.92, 'GBP': 0.79}\n"
        "usd = 1000\n"
        "converted = {}\n"
        "for code in rates:\n"
        "    converted[code] = round(usd * rates[code], 2)\n"
        "total = 0\n"
        "if 'EUR' in converted and math.sin(0) == 0:\n"
        "    for key in converted:\n"
        "        total = total + converted[key]\n"
        "print(converted, total)"
    )
    namespace, output = safe_exec(code)
    assert namespace["converted"] == {"EUR": 920.0, "GBP": 790.0}
    assert namespace["total"] == pytest.approx(1710.0)
    assert "EUR" in output


def test_method_calls_on_variables_rejected():
    # 方法调用是属性访问的特例，可链到 __class__，必须拒绝（白名单预期约束）
    with pytest.raises(UnsafeCode):
        safe_exec("x = [1, 2]\nresult = x.append(3)")
    with pytest.raises(UnsafeCode):
        safe_exec("d = {}\nresult = d.values()")


def test_runtime_errors_propagate_normally():
    # 未定义变量 → 普通 NameError（运行期错误），而不是 UnsafeCode
    with pytest.raises(NameError):
        safe_exec("result = undefined_var + 1")
    with pytest.raises(ZeroDivisionError):
        safe_exec("result = 1 / 0")


def test_empty_and_invalid_code():
    with pytest.raises(ValueError):
        safe_exec("   ")
    with pytest.raises(ValueError):
        safe_exec("this is not python (((")


# ---------------------------------------------------------------------------
# 集成：ToolRegistry.code_interpreter 的对外行为
# ---------------------------------------------------------------------------
def test_code_interpreter_rejects_escape():
    result = ToolRegistry.code_interpreter(
        "x = ().__class__.__mro__[1].__subclasses__()"
    )
    assert result["success"] is False
    assert "UnsafeCode" in result["error"] or "unsupported syntax" in result["error"]


def test_code_interpreter_runs_legit_code():
    result = ToolRegistry.code_interpreter(
        "amounts = [100, 200, 300]\ntotal = sum(amounts)\nprint('done')"
    )
    assert result["success"] is True
    assert result["result"] == 600
    assert result["output"] == "done\n"
    assert result["variables"]["amounts"] == [100, 200, 300]
