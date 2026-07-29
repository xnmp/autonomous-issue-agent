import { describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./client";

describe("validated API client", () => {
  it("rejects a successful response that violates the generated contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ repos: "not-an-array", warnings: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api.repos()).rejects.toEqual(
      expect.objectContaining<ApiError>({
        name: "ApiError",
        message: expect.stringContaining("invalid response"),
        status: 200,
      }),
    );
  });
});
