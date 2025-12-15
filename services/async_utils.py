import asyncio
from typing import Any, Callable, Iterable, List, Sequence


async def run_in_threads(func: Callable[..., Any], args_list: Iterable[Sequence[Any]], max_concurrency: int = 5) -> List[Any]:
    """
    Run a sync function 'func' over a list/iterable of argument sequences concurrently
    using asyncio.to_thread, bounded by max_concurrency. Returns results in order.
    """
    sem = asyncio.Semaphore(max_concurrency)

    async def _run_one(args: Sequence[Any]) -> Any:
        async with sem:
            return await asyncio.to_thread(func, *args)

    tasks = [asyncio.create_task(_run_one(args)) for args in args_list]
    return await asyncio.gather(*tasks)


async def run_single_arg(func: Callable[[Any], Any], items: Iterable[Any], max_concurrency: int = 5) -> List[Any]:
    """
    Convenience wrapper when func takes a single positional argument.
    """
    sem = asyncio.Semaphore(max_concurrency)

    async def _run_one(item: Any) -> Any:
        async with sem:
            return await asyncio.to_thread(func, item)

    tasks = [asyncio.create_task(_run_one(item)) for item in items]
    return await asyncio.gather(*tasks)
