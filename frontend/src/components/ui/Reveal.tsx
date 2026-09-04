import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

/**
 * Once-only scroll reveal.
 *
 * Uses a native IntersectionObserver that disconnects immediately after the
 * element first enters the viewport, so the animation plays exactly once and
 * can never replay, blink, or flicker when the user scrolls away and back.
 *
 * The transition mirrors the landing page's existing motion language
 * (fade + slight slide, smooth easing) and respects prefers-reduced-motion.
 */
export function Reveal({
  children,
  className,
  delay = 0,
  duration = 0.5,
  from = 'up',
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
  duration?: number;
  from?: 'up' | 'left' | 'none';
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [revealed, setRevealed] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    // Reduced motion: render final state immediately, no observation needed.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setRevealed(true);
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setRevealed(true);
          // Play once → stop observing so it can never re-trigger.
          observer.disconnect();
        }
      },
      { threshold: 0.15 }
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const hiddenTransform =
    from === 'up' ? 'translateY(20px)' : from === 'left' ? 'translateX(-20px)' : 'none';

  const style: CSSProperties = {
    opacity: revealed ? 1 : 0,
    transform: revealed ? 'none' : hiddenTransform,
    transition: `opacity ${duration}s cubic-bezier(0.22, 1, 0.36, 1) ${delay}s, transform ${duration}s cubic-bezier(0.22, 1, 0.36, 1) ${delay}s`,
    willChange: revealed ? 'auto' : 'opacity, transform',
  };

  return (
    <div ref={ref} className={className} style={style}>
      {children}
    </div>
  );
}