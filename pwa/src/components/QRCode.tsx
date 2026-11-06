import { QRCodeSVG } from "qrcode.react";

interface Props {
  value: string;
  size?: number;
  label?: string;
}

export function QRCode({ value, size = 220, label }: Props) {
  return (
    <figure className="qr">
      <QRCodeSVG value={value} size={size} level="M" includeMargin bgColor="#ffffff" fgColor="#000000" />
      {label && <figcaption>{label}</figcaption>}
    </figure>
  );
}
