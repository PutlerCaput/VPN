import { endpoints, isApiError } from "./api/client";
import { ru } from "./i18n/ru";
import { getWebApp, haptic, notify, openBotChat } from "./telegram";

/** Errors the user can act on. Anything else falls back to the bot chat link. */
const SHOWN: Record<string, string> = {
  offer_not_accepted: ru.payment.errors.offer,
  invalid_tier: ru.payment.errors.tariff,
  invalid_months: ru.payment.errors.tariff,
  rate_limited: ru.payment.errors.rateLimited,
  payments_unavailable: ru.payment.errors.unavailable,
  banned: ru.payment.errors.banned,
};

/**
 * Payments live in the bot. The server posts the payment menu to the chat and the
 * Mini App closes, so the user sees the menu at once. A t.me link alone no longer
 * closes the Mini App (Bot API 7.0+): the chat opened underneath and the button
 * looked dead.
 */
export async function startPayment(tier?: string, months?: number): Promise<void> {
  try {
    await endpoints.payInBot(tier, months);
    haptic("success");
    const app = getWebApp();
    if (app) app.close();
    else notify(ru.payment.sent);
    return;
  } catch (err) {
    const message = isApiError(err) ? SHOWN[err.message] : undefined;
    if (message) {
      haptic("error");
      notify(message);
      return;
    }
  }
  // The server could not reach the chat: open it with a deep link instead.
  const start = tier && months ? `pay_${tier}_${months}` : tier ? `pay_${tier}` : "pay";
  if (openBotChat(start)) {
    window.setTimeout(() => getWebApp()?.close(), 300);
    return;
  }
  haptic("error");
  notify(ru.payment.errors.generic);
}
