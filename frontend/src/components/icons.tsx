// 纯装饰性线性图标：统一 1.6 描边、currentColor 取色，跟随文字颜色变化。
type IconProps = { size?: number };

function svg(size: number) {
  return {
    width: size, height: size, viewBox: '0 0 24 24', fill: 'none' as const,
    stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const, 'aria-hidden': true, focusable: 'false' as const,
  };
}

export function BrandMark({ size = 22 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <circle cx="12" cy="12" r="9" opacity=".45" />
      <path d="M12 6.5 14.2 11 12 17.5 9.8 11z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function ChatIcon({ size = 17 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M20 12.5c0 3.6-3.6 6.5-8 6.5-1 0-2-.15-2.9-.43L5 20l1.1-3.1A7.2 7.2 0 0 1 4 12.5C4 8.9 7.6 6 12 6s8 2.9 8 6.5z" />
    </svg>
  );
}

export function PlusIcon({ size = 16 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M12 5.5v13M5.5 12h13" />
    </svg>
  );
}

export function EditIcon({ size = 15 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M4.5 19.5h4l9-9a2.1 2.1 0 0 0-3-3l-9 9v3z" />
      <path d="M13.5 7.5l3 3" />
    </svg>
  );
}

export function CloseIcon({ size = 15 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M6.5 6.5l11 11M17.5 6.5l-11 11" />
    </svg>
  );
}

export function ArrowUpIcon({ size = 17 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M12 18.5V6.5M7 11.5l5-5 5 5" />
    </svg>
  );
}

export function MenuIcon({ size = 18 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M4 7.5h16M4 12h16M4 16.5h11" />
    </svg>
  );
}

export function AlertIcon({ size = 16 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 8v5M12 16h.01" />
    </svg>
  );
}

export function TableIcon({ size = 16 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <rect x="4" y="5.5" width="16" height="13" rx="2.5" />
      <path d="M4 10h16M10 10v8.5" />
    </svg>
  );
}

export function GaugeIcon({ size = 16 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M4.5 16a8 8 0 1 1 15 0" />
      <path d="M12 16l4-5" />
    </svg>
  );
}

export function ClockIcon({ size = 14 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 7.5V12l3 1.8" />
    </svg>
  );
}

export function BellIcon({ size = 15 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 1.5H5z" />
      <path d="M10 20.5a2.2 2.2 0 0 0 4 0" />
    </svg>
  );
}

export function ReviewIcon({ size = 15 }: IconProps) {
  return (
    <svg {...svg(size)}>
      <path d="M12 3.5 19 6v5.5c0 4.2-3 7.4-7 9-4-1.6-7-4.8-7-9V6z" />
      <path d="M9 12l2.2 2.2L15.5 10" />
    </svg>
  );
}
