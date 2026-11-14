import { endpoints } from "../api/client";
import { EmailCodeForm } from "../auth/EmailCodeForm";
import { TelegramLogin } from "../auth/TelegramLogin";
import { useAppStore } from "../store";
import { InstallHint } from "../components/InstallHint";

export function LoginPage() {
  const setTokens = useAppStore((s) => s.setTokens);
  return (
    <main className="page center">
      <img className="splash-logo" src="/brand/logo-512.webp" width={220} height={220} alt="Cyberdefency" />
      <h1>Шифрование подключения в публичных сетях</h1>
      <p className="muted">
        Персональный технический узел для работы в Wi-Fi кафе, коворкингов, отелей и аэропортов. Протокол VLESS +
        Reality. Первые 24 часа бесплатно.
      </p>
      <TelegramLogin />
      <section className="card">
        <h2>Вход по почте</h2>
        <p className="small muted">Если Telegram не открывается: пришлём код на почту.</p>
        <EmailCodeForm
          submitLabel="Войти"
          request={endpoints.requestEmailCode}
          confirm={async (email, code) => {
            const pair = await endpoints.loginWithEmailCode(email, code);
            setTokens(pair.access_token, pair.refresh_token);
          }}
        />
      </section>
      <p className="small muted">
        Вход через Telegram: мы получаем только ваш Telegram ID, имя и username. Вход по почте: только адрес. 18+. Сервис не предназначен для доступа
        к ресурсам, ограниченным законодательством РФ, и не скрывает личность пользователя.
      </p>
      <InstallHint />
    </main>
  );
}
