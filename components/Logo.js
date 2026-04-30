import Image from 'next/image';

/**
 * Site logo backed by Next.js Image for automatic optimization and lazy-loading.
 * Always pass explicit width/height (or use fill + a sized container) to avoid
 * Cumulative Layout Shift.
 */
export default function Logo({ width = 120, height = 40 }) {
  return (
    <Image
      src="/logo.svg"
      alt="AI Coworker logo"
      width={width}
      height={height}
      priority
    />
  );
}
