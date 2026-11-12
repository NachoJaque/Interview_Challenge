const currency = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const dateTime = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeStyle: "short" });

export const money = (amount: string | number) => currency.format(Number(amount));
export const formatDate = (iso: string) => dateTime.format(new Date(iso));

export function timeUntil(iso: string): string {
  const hours = (new Date(iso).getTime() - Date.now()) / 3_600_000;
  if (hours <= 0) return "already started";
  if (hours < 1) return "starts in less than an hour";
  if (hours < 48) return `starts in ${Math.floor(hours)} hours`;
  return `starts in ${Math.floor(hours / 24)} days`;
}

export function clock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
