import { useState, type FormEvent } from "react";

import { ApiError } from "../api/client";

interface Props {
  /** Asks the server to mail a code to the address. */
  request: (email: string) => Promise<unknown>;
  /** Hands the code in; resolves when it was right. */
  confirm: (email: string, code: string) => Promise<unknown>;
  submitLabel: string;
}

function messageFor(err: unknown, step: "email" | "code"): string {
  if (err instanceof ApiError) {
    if (err.status === 429) return "Слишком много попыток. Подождите и попробуйте снова.";
    if (err.status === 503) return "Вход по почте сейчас недоступен.";
    if (err.status === 502) return "Не удалось отправить письмо. Попробуйте позже.";
    if (err.status === 409) return "К учётной записи уже привязан адрес.";
    if (step === "email") return "Проверьте адрес почты.";
    return "Код не подошёл или устарел. Запросите новый.";
  }
  return "Нет связи с сервером. Попробуйте ещё раз.";
}

/** Two steps: the address, then the six digits from the letter. */
export function EmailCodeForm({ request, confirm, submitLabel }: Props) {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (event: FormEvent, action: () => Promise<unknown>, after?: () => void) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await action();
      after?.();
    } catch (err) {
      setError(messageFor(err, step));
    } finally {
      setBusy(false);
    }
  };

  if (step === "email") {
    return (
      <form className="email-form" onSubmit={(e) => void run(e, () => request(email.trim()), () => setStep("code"))}>
        <input
          type="email"
          inputMode="email"
          autoComplete="email"
          required
          maxLength={254}
          placeholder="Адрес почты"
          aria-label="Адрес почты"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <button type="submit" className="btn primary" disabled={busy}>
          Получить код
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    );
  }
  return (
    <form className="email-form" onSubmit={(e) => void run(e, () => confirm(email.trim(), code))}>
      <p className="small muted">Письмо с кодом отправлено на {email.trim()}. Код действует 10 минут.</p>
      <input
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        required
        maxLength={6}
        placeholder="Код из письма"
        aria-label="Код из письма"
        value={code}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
      />
      <button type="submit" className="btn primary" disabled={busy || code.length !== 6}>
        {submitLabel}
      </button>
      <button
        type="button"
        className="btn link"
        onClick={() => {
          setStep("email");
          setCode("");
          setError(null);
        }}
      >
        Другой адрес или новый код
      </button>
      {error && <p className="error">{error}</p>}
    </form>
  );
}
