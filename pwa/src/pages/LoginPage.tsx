import { TelegramLogin } from "../auth/TelegramLogin";
import { InstallHint } from "../components/InstallHint";

export function LoginPage() {
  return (
    <main className="page center">
      <div className="logo" aria-hidden="true">🔐</div>
      <h1>Шифрование подключения в публичных сетях</h1>
      <p className="muted">
        Персональный технический узел для работы в Wi-Fi кафе, коворкингов, отелей и аэропортов. Протокол VLESS +
        Reality. Первые 24 часа бесплатно.
      </p>
      <TelegramLogin />
      <p className="small muted">
        Вход через Telegram: мы получаем только ваш Telegram ID, имя и username. 18+. Сервис не предназначен для доступа
        к ресурсам, ограниченным законодательством РФ, и не скрывает личность пользователя.
      </p>
      <InstallHint />
    </main>
  );
}
