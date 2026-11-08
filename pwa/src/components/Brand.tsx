import type { ReactNode } from "react";

/** The emblem and the wordmark as in the logo: "Cyber" white, "defency" blue. */
export function BrandHeader() {
  return (
    <header className="brand">
      <img className="emblem" src="/brand/emblem-128.webp" width={40} height={40} alt="" />
      <span className="wordmark">
        Cyber<span className="wordmark-accent">defency</span>
      </span>
    </header>
  );
}

/** The full logo for loading and sign-in screens. */
export function Splash({ children }: { children?: ReactNode }) {
  return (
    <main className="splash">
      <img className="splash-logo" src="/brand/logo-512.webp" width={220} height={220} alt="Cyberdefency" />
      {children}
    </main>
  );
}
