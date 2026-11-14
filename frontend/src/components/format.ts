import type { InsuranceType } from "../api/types";

export const INSURANCE_LABELS: Record<InsuranceType, string> = {
  HEALTH: "Health",
  MOTOR: "Motor",
  TRAVEL: "Travel",
  HOME: "Home",
};

const dateFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

export function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : dateFormat.format(date);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatPages(start: number, end: number): string {
  return start === end ? `Page ${start}` : `Pages ${start}–${end}`;
}

export function formatSeconds(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(1)} s`;
}
