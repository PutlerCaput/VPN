import type { ReactNode } from "react";

import ui from "../styles/components.module.css";

const BASE = import.meta.env.BASE_URL;

/** The emblem and the wordmark as in the logo: "Cyber" white, "defency" blue. */
export function BrandHeader() {
  return (
    <header className={ui.brand}>
      <img className={ui.emblem} src={`${BASE}brand/emblem-128.webp`} width={38} height={38} alt="" />
      <span className={ui.wordmark}>
        Cyber<span className={ui.wordmarkAccent}>defency</span>
      </span>
    </header>
  );
}

/** The full logo for loading, sign-in and session screens. */
export function Splash({ children }: { children?: ReactNode }) {
  return (
    <div className={ui.splash}>
      <img className={ui.splashLogo} src={`${BASE}brand/logo-512.webp`} width={220} height={220} alt="Cyberdefency" />
      {children}
    </div>
  );
}
