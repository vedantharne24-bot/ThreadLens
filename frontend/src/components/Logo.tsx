/** ThreadLens mark: three thread strands converging through a lens ring. */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <defs>
        <linearGradient id="tl-g" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop stopColor="var(--cyan)" />
          <stop offset="1" stopColor="var(--violet)" />
        </linearGradient>
      </defs>
      <circle cx="16" cy="16" r="11" stroke="url(#tl-g)" strokeWidth="2.2" />
      <path d="M3 9.5c6 0 8 6.5 13 6.5s7-6.5 13-6.5" stroke="var(--cyan)" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M3 16h26" stroke="var(--fg-2)" strokeWidth="1.8" strokeLinecap="round" />
      <path d="M3 22.5c6 0 8-6.5 13-6.5s7 6.5 13 6.5" stroke="var(--violet)" strokeWidth="1.8" strokeLinecap="round" />
      <circle cx="16" cy="16" r="2.6" fill="var(--bg)" stroke="var(--crit)" strokeWidth="1.6" />
    </svg>
  );
}
