import type { CSSProperties, ReactNode } from "react";
import { motion } from "motion/react";

interface Props {
  children: ReactNode;
  className?: string;
  /** Slide in from the left or right instead of from below. */
  from?: "below" | "left" | "right";
  delay?: number;
  as?: "div" | "section" | "li";
  style?: CSSProperties;
}

const offset = { below: { y: 40 }, left: { x: -48 }, right: { x: 48 } };

/** Fades a block in the first time it scrolls into view. Honours reduced motion (see MotionConfig in App). */
export function Reveal({ children, className, from = "below", delay = 0, as = "div", style }: Props) {
  const M = motion[as];
  return (
    <M
      className={className}
      style={style}
      initial={{ opacity: 0, ...offset[from] }}
      whileInView={{ opacity: 1, x: 0, y: 0 }}
      viewport={{ once: true, margin: "0px 0px -12% 0px" }}
      transition={{ duration: 0.7, ease: [0.2, 0.75, 0.2, 1], delay }}
    >
      {children}
    </M>
  );
}
