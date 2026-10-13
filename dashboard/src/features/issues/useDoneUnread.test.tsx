import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DoneCard } from "../../api/types";
import { useDoneUnread } from "./useDoneUnread";

const KEY = "done-seen:r";
const NOW = Date.parse("2026-07-30T12:00:00Z");

const card = (issue: number, merged_ts: string | null): DoneCard => ({
  issue, title: `t${issue}`, url: null, pr: 1, pr_url: null, merged_ts,
  cost_usd: 0, legs: 0, review_rounds: 0, labels: [],
});
const at = (iso: string) => String(Date.parse(iso));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
});

type Props = { cards: DoneCard[] | undefined; viewing: boolean };
const setup = (initial: Props, repo = "r") =>
  renderHook((p: Props) => useDoneUnread(repo, p.cards, p.viewing), { initialProps: initial });

const OLD = card(1, "2026-07-29T00:00:00Z");
const FRESH = card(2, "2026-07-30T08:00:00Z");

describe("useDoneUnread", () => {
  it("shows nothing as new on the first ever visit", () => {
    const { result } = setup({ cards: [OLD, FRESH], viewing: false });
    expect(result.current.count).toBe(0);
    expect(Number(localStorage.getItem(KEY))).toBe(NOW);
  });

  it("counts and highlights items done after the watermark", () => {
    localStorage.setItem(KEY, at("2026-07-30T00:00:00Z"));
    const { result, rerender } = setup({ cards: [OLD, FRESH], viewing: false });
    expect(result.current.count).toBe(1);
    expect(result.current.unread.size).toBe(0);
    rerender({ cards: [OLD, FRESH], viewing: true });
    expect([...result.current.unread]).toEqual([2]);
  });

  it("clears the badge on opening but keeps highlights for the visit, then none on revisit", () => {
    localStorage.setItem(KEY, at("2026-07-30T00:00:00Z"));
    const cards = [OLD, FRESH];
    const { result, rerender } = setup({ cards, viewing: false });
    rerender({ cards, viewing: true });
    expect(result.current.count).toBe(0);
    expect([...result.current.unread]).toEqual([2]);
    expect(localStorage.getItem(KEY)).toBe(at("2026-07-30T08:00:00Z"));
    rerender({ cards, viewing: false });
    expect(result.current.count).toBe(0);
    rerender({ cards, viewing: true });
    expect(result.current.unread.size).toBe(0);
  });

  it("treats items arriving while the tab is open as seen", () => {
    localStorage.setItem(KEY, at("2026-07-30T00:00:00Z"));
    const { result, rerender } = setup({ cards: [OLD, FRESH], viewing: true });
    const arrival = card(3, "2026-07-30T11:00:00Z");
    rerender({ cards: [arrival, OLD, FRESH], viewing: true });
    expect([...result.current.unread]).toEqual([2]);
    rerender({ cards: [arrival, OLD, FRESH], viewing: false });
    expect(result.current.count).toBe(0);
  });

  it("waits for data before freezing the unread set", () => {
    localStorage.setItem(KEY, at("2026-07-30T00:00:00Z"));
    const { result, rerender } = setup({ cards: undefined, viewing: true });
    expect(result.current.unread.size).toBe(0);
    rerender({ cards: [FRESH], viewing: true });
    expect([...result.current.unread]).toEqual([2]);
  });

  it.each(["garbage", "", "-5", "0", "NaN", "Infinity"])("treats stored %j as a first visit", (stored) => {
    localStorage.setItem(KEY, stored);
    const { result } = setup({ cards: [OLD, FRESH], viewing: false });
    expect(result.current.count).toBe(0);
    expect(Number(localStorage.getItem(KEY))).toBe(NOW);
  });

  it("never treats an item with a missing or unparseable timestamp as new", () => {
    localStorage.setItem(KEY, at("2026-07-01T00:00:00Z"));
    const { result, rerender } = setup({ cards: [card(5, null), card(6, "not a date")], viewing: false });
    expect(result.current.count).toBe(0);
    rerender({ cards: [card(5, null), card(6, "not a date")], viewing: true });
    expect(result.current.unread.size).toBe(0);
  });

  it("keeps working when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("full"); });
    const cards = [OLD, FRESH];
    const { result, rerender } = setup({ cards, viewing: false });
    expect(result.current.count).toBe(0);
    const later = card(9, "2026-07-30T13:00:00Z");
    rerender({ cards: [later, ...cards], viewing: false });
    expect(result.current.count).toBe(1);
    rerender({ cards: [later, ...cards], viewing: true });
    expect([...result.current.unread]).toEqual([9]);
  });

  it("keeps each repo's watermark separate", () => {
    localStorage.setItem("done-seen:a", at("2026-07-30T00:00:00Z"));
    localStorage.setItem("done-seen:b", at("2026-07-30T10:00:00Z"));
    const a = setup({ cards: [FRESH], viewing: false }, "a");
    const b = setup({ cards: [FRESH], viewing: false }, "b");
    expect([a.result.current.count, b.result.current.count]).toEqual([1, 0]);
  });
});
