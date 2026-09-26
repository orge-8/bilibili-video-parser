"""pytest 配置：无 pytest-asyncio 依赖，异步用例用 asyncio.run 执行。"""

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem):
    """把 async 测试函数用 asyncio.run 跑掉（devkit venv 无 pytest-asyncio）。"""
    testfunction = pyfuncitem.obj
    if not inspect.iscoroutinefunction(testfunction):
        return None
    funcargs = pyfuncitem.funcargs
    testargs = {name: funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}

    async def runner(**kwargs):
        return await testfunction(**kwargs)

    asyncio.run(runner(**testargs))
    return True
