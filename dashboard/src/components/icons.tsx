import type { SVGProps } from 'react'

/**
 * Inline 24px stroke icons. Shipping them here keeps the bundle free of an
 * icon dependency and lets every glyph inherit `currentColor`.
 */
const base = (props: SVGProps<SVGSVGElement>): SVGProps<SVGSVGElement> => ({
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
  focusable: false,
  width: '1em',
  height: '1em',
  ...props,
})

export const ChevronLeftIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <path d="m15 18-6-6 6-6" />
  </svg>
)

export const ChevronRightIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <path d="m9 18 6-6-6-6" />
  </svg>
)

export const RefreshIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </svg>
)

export const SunIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
)

export const MoonIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
  </svg>
)

export const MonitorIcon = (props: SVGProps<SVGSVGElement>) => (
  <svg {...base(props)}>
    <rect x="2" y="4" width="20" height="13" rx="2" />
    <path d="M8 21h8M12 17v4" />
  </svg>
)
