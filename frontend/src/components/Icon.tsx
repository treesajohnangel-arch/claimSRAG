// A small authored icon set: one 24px grid, one 1.75 stroke, round joins. Icons are decorative (aria-hidden): every
// control or status that shows one also has a text label.

const PATHS = {
  overview: "M4 5h7v6H4zM13 5h7v4h-7zM13 11h7v8h-7zM4 13h7v6H4z",
  policies: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6",
  search: "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13zM15.5 15.5 20 20",
  ask: "M5 5h14v10H10l-5 4zM9 9h6M9 12h4",
  plus: "M12 5v14M5 12h14",
  answered: "M7 3h7l5 5v13H7zM14 3v5h5M9.5 14.5l2 2 4-4.5",
  conflict: "M12 4 3 20h18zM12 10v4.5M12 17.5v.01",
  abstained: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM5.6 5.6l12.8 12.8",
  document: "M7 3h7l5 5v13H7zM14 3v5h5",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6 6 18",
  chevron: "M9 6l6 6-6 6",
  retry: "M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5",
  edit: "M4 20h4L19 9l-4-4L4 16zM13 7l4 4",
  trash: "M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8v.01",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6z",
  upload: "M12 16V4M7 9l5-5 5 5M5 20h14",
  documents: "M8 7V4h8l4 4v10h-3M4 8h8l4 4v8H4zM12 8v4h4",
  shield: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6zM9 12l2 2 4-4",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  arrowRight: "M5 12h14M13 6l6 6-6 6",
  page: "M6 3h9l3 3v15H6zM9 9h6M9 13h6M9 17h4",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 20, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
