import type { SVGProps } from "react";

export type IconName =
  | "agent"
  | "alert-triangle"
  | "chevron-left"
  | "chevron-right"
  | "check-circle"
  | "close"
  | "copy"
  | "git-pull-request"
  | "inbox"
  | "play"
  | "settings";

const paths: Record<IconName, React.ReactNode> = {
  agent: (
    <>
      <path d="M4 15.5V9.8a3.8 3.8 0 0 1 3.8-3.8h8.4A3.8 3.8 0 0 1 20 9.8v5.7a3.5 3.5 0 0 1-3.5 3.5h-9A3.5 3.5 0 0 1 4 15.5Z" />
      <path d="M12 6V3m-2 0h4M8.5 12h.01m6.99 0h.01M9 16h6" />
    </>
  ),
  "alert-triangle": (
    <>
      <path d="M10.3 3.6 2.7 17a2 2 0 0 0 1.8 3h15a2 2 0 0 0 1.8-3L13.7 3.6a2 2 0 0 0-3.4 0Z" />
      <path d="M12 8v4m0 3h.01" />
    </>
  ),
  "chevron-left": <path d="m14.5 6-6 6 6 6" />,
  "chevron-right": <path d="m9.5 6 6 6-6 6" />,
  "check-circle": (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m8.2 12.1 2.5 2.5 5.2-5.4" />
    </>
  ),
  close: <path d="m7.5 7.5 9 9m0-9-9 9" />,
  copy: (
    <>
      <rect x="8" y="8" width="11" height="11" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </>
  ),
  "git-pull-request": (
    <>
      <circle cx="6" cy="6" r="2.6" />
      <circle cx="18" cy="18" r="2.6" />
      <path d="M6 8.6V21M13 6h3a2 2 0 0 1 2 2v7.4" />
      <path d="m15.5 3.5-2.5 2.5 2.5 2.5" />
    </>
  ),
  inbox: (
    <>
      <path d="M4 7.5 6.2 4h11.6L20 7.5V19H4V7.5Z" />
      <path d="M4 13h4l1.5 2h5l1.5-2h4" />
    </>
  ),
  play: <path d="m9 7 8 5-8 5V7Z" />,
  settings: (
    <>
      <circle cx="12" cy="12" r="2.7" />
      <path d="M19.2 13.5v-3l-2-.7a7 7 0 0 0-.7-1.6l.9-1.9-2.1-2.1-1.9.9a7 7 0 0 0-1.6-.7l-.7-2h-3l-.7 2a7 7 0 0 0-1.6.7l-1.9-.9-2.1 2.1.9 1.9a7 7 0 0 0-.7 1.6l-2 .7v3l2 .7a7 7 0 0 0 .7 1.6l-.9 1.9 2.1 2.1 1.9-.9a7 7 0 0 0 1.6.7l.7 2h3l.7-2a7 7 0 0 0 1.6-.7l1.9.9 2.1-2.1-.9-1.9a7 7 0 0 0 .7-1.6l2-.7Z" />
    </>
  ),
};

export function Icon({
  name,
  size = 16,
  ...props
}: SVGProps<SVGSVGElement> & { name: IconName; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
