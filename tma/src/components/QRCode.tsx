import { QRCodeSVG } from "qrcode.react";

import styles from "../styles/components.module.css";

export function QRCode({ value, size = 220 }: { value: string; size?: number }) {
  return (
    <div className={styles.qr}>
      <QRCodeSVG value={value} size={size} level="M" includeMargin bgColor="#ffffff" fgColor="#000000" />
    </div>
  );
}
