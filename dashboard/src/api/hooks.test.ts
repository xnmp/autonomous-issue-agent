import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { POLL_TIMEOUT_MS, usePolled } from "./hooks";


interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}


function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}


afterEach(() => {
  vi.useRealTimers();
});


describe("usePolled authority reads", () => {
  it("does not reuse a cached result when an uncached reader mounts", async () => {
    const key = "authority/remount";
    const primed = renderHook(() =>
      usePolled(key, async () => "cached", 0),
    );
    await waitFor(() => expect(primed.result.current.data).toBe("cached"));
    primed.unmount();

    const fresh = deferred<string>();
    const fetcher = vi.fn(() => fresh.promise);
    const mounted = renderHook(() =>
      usePolled(key, fetcher, 0, { cache: false }),
    );

    expect(fetcher).toHaveBeenCalledOnce();
    expect(mounted.result.current).toEqual(expect.objectContaining({
      data: null,
      error: null,
      loading: true,
      updatedAt: null,
    }));

    await act(async () => {
      fresh.resolve("fresh");
      await fresh.promise;
    });
    expect(mounted.result.current.data).toBe("fresh");
    expect(mounted.result.current.loading).toBe(false);
  });

  it("does not reuse either resource's cached result when its key changes", async () => {
    const cachedKey = "authority/switch/cached";
    const primed = renderHook(() =>
      usePolled(cachedKey, async () => "cached-other-resource", 0),
    );
    await waitFor(() => expect(primed.result.current.data).toBe("cached-other-resource"));
    primed.unmount();

    const reads = new Map([
      ["authority/switch/first", deferred<string>()],
      [cachedKey, deferred<string>()],
    ]);
    const fetcher = vi.fn((_signal: AbortSignal, key: string) => reads.get(key)!.promise);
    const mounted = renderHook(
      ({ resource }) => usePolled(
        resource,
        (signal) => fetcher(signal, resource),
        0,
        { cache: false },
      ),
      { initialProps: { resource: "authority/switch/first" } },
    );

    await act(async () => {
      const first = reads.get("authority/switch/first")!;
      first.resolve("fresh-first");
      await first.promise;
    });
    expect(mounted.result.current.data).toBe("fresh-first");

    mounted.rerender({ resource: cachedKey });

    expect(mounted.result.current).toEqual(expect.objectContaining({
      data: null,
      error: null,
      loading: true,
      updatedAt: null,
    }));
    expect(fetcher).toHaveBeenCalledTimes(2);

    await act(async () => {
      const second = reads.get(cachedKey)!;
      second.resolve("fresh-second");
      await second.promise;
    });
    expect(mounted.result.current.data).toBe("fresh-second");
  });
});


describe("usePolled request ordering", () => {
  it("serializes interval reads while the previous read is pending", async () => {
    vi.useFakeTimers();
    const first = deferred<string>();
    const second = deferred<string>();
    const fetcher = vi.fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const mounted = renderHook(() =>
      usePolled("serialized-interval", fetcher, 1_000, { cache: false }),
    );

    expect(fetcher).toHaveBeenCalledOnce();
    act(() => vi.advanceTimersByTime(5_000));
    expect(fetcher).toHaveBeenCalledOnce();

    await act(async () => {
      first.resolve("first");
      await first.promise;
    });
    expect(mounted.result.current.data).toBe("first");

    act(() => vi.advanceTimersByTime(1_000));
    expect(fetcher).toHaveBeenCalledTimes(2);
    act(() => vi.advanceTimersByTime(5_000));
    expect(fetcher).toHaveBeenCalledTimes(2);

    await act(async () => {
      second.resolve("second");
      await second.promise;
    });
    expect(mounted.result.current.data).toBe("second");
  });

  it("aborts a changed key and ignores its late response", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn((signal: AbortSignal, key: string) => {
      signals.push(signal);
      return key === "stale-key" ? first.promise : second.promise;
    });
    const mounted = renderHook(
      ({ resource }) => usePolled(
        resource,
        (signal) => fetcher(signal, resource),
        0,
        { cache: false },
      ),
      { initialProps: { resource: "stale-key" } },
    );

    mounted.rerender({ resource: "current-key" });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(signals[0]!.aborted).toBe(true);
    expect(signals[1]!.aborted).toBe(false);

    await act(async () => {
      second.resolve("current");
      await second.promise;
    });
    await act(async () => {
      first.resolve("stale");
      await first.promise;
    });

    expect(mounted.result.current.data).toBe("current");
    expect(mounted.result.current.error).toBeNull();
  });

  it("aborts a refreshed read and ignores its late response", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return signals.length === 1 ? first.promise : second.promise;
    });
    const mounted = renderHook(() =>
      usePolled("manual-refresh", fetcher, 0, { cache: false }),
    );

    act(() => mounted.result.current.refresh());
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(signals[0]!.aborted).toBe(true);
    expect(signals[1]!.aborted).toBe(false);

    await act(async () => {
      second.resolve("current");
      await second.promise;
    });
    await act(async () => {
      first.resolve("stale");
      await first.promise;
    });

    expect(mounted.result.current.data).toBe("current");
    expect(mounted.result.current.error).toBeNull();
  });
});


describe("usePolled hung requests", () => {
  it("bounds a request that never settles so the poll keeps running", async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const fetcher = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<string>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason));
      });
    });
    const polled = renderHook(() => usePolled("hung/request", fetcher, 5_000));
    expect(fetcher).toHaveBeenCalledTimes(1);

    // Ticks while the first request hangs do not stack concurrent requests.
    await act(async () => { await vi.advanceTimersByTimeAsync(POLL_TIMEOUT_MS - 1); });
    expect(fetcher).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(signals[0]!.aborted).toBe(true);
    expect(polled.result.current.error).toMatch(/timed out/);

    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(fetcher).toHaveBeenCalledTimes(2);
    polled.unmount();
  });

  it("treats unmount as cancellation, not as a timeout error", async () => {
    vi.useFakeTimers();
    let seen: AbortSignal | null = null;
    const fetcher = vi.fn((signal: AbortSignal) => {
      seen = signal;
      return new Promise<string>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason));
      });
    });
    const polled = renderHook(() => usePolled("hung/unmount", fetcher, 5_000));
    polled.unmount();
    expect(seen!.aborted).toBe(true);
    expect((seen!.reason as DOMException).name).toBe("AbortError");
  });
});
