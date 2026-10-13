import { describe, expect, it } from "vitest";
import { legacyUnblockRefusal } from "./legacy-unblock";

describe("legacyUnblockRefusal", () => {
  const families = (...active: ("resolve" | "review_rejections")[]) => ({ status: "available" as const, repository: "o/r", active_families: active });

  it("allows the legacy unblock when the repository read shows resolve control inactive", () => {
    expect(legacyUnblockRefusal(families())).toBeNull();
  });

  it("ignores a retained review-family epoch, which no longer holds anything", () => {
    expect(legacyUnblockRefusal(families("review_rejections"))).toBeNull();
  });

  it("retires the legacy unblock when resolve control is active", () => {
    expect(legacyUnblockRefusal(families("resolve", "review_rejections"))).toMatch(/Resolve control is active/);
  });

  it("never treats an unread or unavailable family status as none active", () => {
    expect(legacyUnblockRefusal(null)).toMatch(/not confirmed/);
    expect(legacyUnblockRefusal({ status: "unavailable", reason: "database is locked" })).toMatch(/unavailable \(database is locked\)/);
  });
});
