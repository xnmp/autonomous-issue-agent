import type { SVGProps } from "react";

export type IconName =
  | "agent"
  | "chevron-left"
  | "chevron-right"
  | "check-circle"
  | "close"
  | "play"
  | "settings";

const paths: Record<IconName, React.ReactNode> = {
  agent: (
    <>
      <path d="M4 15.5V9.8a3.8 3.8 0 0 1 3.8-3.8h8.4A3.8 3.8 0 0 1 20 9.8v5.7a3.5 3.5 0 0 1-3.5 3.5h-9A3.5 3.5 0 0 1 4 15.5Z" />
      <path d="M12 6V3m-2 0h4M8.5 12h.01m6.99 0h.01M9 16h6" />
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
