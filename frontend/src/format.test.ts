import { describe, expect, it } from "vitest";
import { errorMessage } from "./api";
import { clock, money, refundPerTicket, timeUntil } from "./format";
import { nextPollDelay } from "./hooks";

describe("refundPerTicket", () => {
  it("pays the full price at 100%", () => {
    expect(refundPerTicket("150.00", 100)).toBe(150);
  });

  it("rounds half a cent up, like the API", () => {
    expect(refundPerTicket("25.25", 50)).toBe(12.63);
    expect(refundPerTicket("0.01", 50)).toBe(0.01);
  });

  it("is exact when the price divides evenly", () => {
    expect(refundPerTicket("35.00", 50)).toBe(17.5);
  });
});

describe("clock", () => {
  it("formats seconds as m:ss", () => {
    expect(clock(600)).toBe("10:00");
    expect(clock(65)).toBe("1:05");
    expect(clock(0)).toBe("0:00");
  });
});

describe("money", () => {
  it("formats decimal strings from the API", () => {
    expect(money("1250.5")).toBe("$1,250.50");
  });
});

describe("timeUntil", () => {
  const inHours = (hours: number) => new Date(Date.now() + hours * 3_600_000 + 60_000).toISOString();

  it("describes how far away an event is", () => {
    expect(timeUntil(inHours(-1))).toBe("already started");
    expect(timeUntil(inHours(0))).toBe("starts in less than an hour");
    expect(timeUntil(inHours(26))).toBe("starts in 26 hours");
    expect(timeUntil(inHours(24 * 30))).toBe("starts in 30 days");
  });
});

describe("errorMessage", () => {
  it("prefers the API's business error, even on 429", () => {
    const body = { error: { code: "too_many_reservations", message: "Please try again later." } };
    expect(errorMessage(429, body)).toEqual({ message: "Please try again later.", code: "too_many_reservations" });
  });

  it("explains the web server's rate limit, which has no JSON body", () => {
    expect(errorMessage(429, null).message).toMatch(/Too many attempts/);
  });

  it("reads validation errors from the same shape", () => {
    const body = { error: { code: "validation_error", message: "password: too short", details: [] } };
    expect(errorMessage(422, body)).toEqual({ message: "password: too short", code: "validation_error" });
  });

  it("falls back to a generic message", () => {
    expect(errorMessage(500, "<html>").message).toBe("Something went wrong. Please try again.");
  });
});

describe("errorMessage for an overloaded site", () => {
  it("explains a 503 that has no JSON body", () => {
    expect(errorMessage(503, null)).toEqual({
      message: "The site is very busy right now. Please try again in a few seconds.",
      code: "service_busy",
    });
  });

  it("still prefers the API's own message", () => {
    const body = { error: { code: "service_busy", message: "Please try again in a few seconds." } };
    expect(errorMessage(503, body).message).toBe("Please try again in a few seconds.");
  });
});

describe("nextPollDelay", () => {
  it("keeps the normal pace while requests succeed", () => {
    expect(nextPollDelay(3000, 0, 0)).toBe(3000);
  });

  it("doubles the wait after each failure, up to a ceiling", () => {
    expect([1, 2, 3].map((failures) => nextPollDelay(3000, failures, 0))).toEqual([6000, 12000, 24000]);
    expect(nextPollDelay(3000, 10, 0)).toBe(30000);
  });

  it("spreads clients out by up to 20%", () => {
    expect(nextPollDelay(3000, 0, 1)).toBe(3600);
  });
});
