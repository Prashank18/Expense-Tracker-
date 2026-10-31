import { withSupabase } from "npm:@supabase/server@1";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

function jsonResponse(body: Record<string, unknown>, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(withSupabase({ auth: "user" }, async (request, context) => {
  if (request.method !== "POST") return jsonResponse({ error: "Method not allowed." }, 405);

  const admin = context.supabaseAdmin;
  const userId = context.userClaims?.id;
  if (!userId) return jsonResponse({ error: "Sign in to continue." }, 401);

  let payload: { category?: unknown };
  try {
    payload = await request.json();
  } catch {
    return jsonResponse({ error: "Invalid request body." }, 400);
  }
  const category = typeof payload.category === "string" ? payload.category : "";
  if (!category || category === "Income") return jsonResponse({ error: "Invalid budget category." }, 400);

  const { data: profile, error: profileError } = await admin.from("profiles")
    .select("whatsapp_phone, whatsapp_opt_in").eq("id", userId).maybeSingle();
  if (profileError) return jsonResponse({ error: "Could not read alert preferences." }, 500);
  if (!profile?.whatsapp_opt_in || !profile.whatsapp_phone) {
    return jsonResponse({ status: "not_opted_in" });
  }

  const now = new Date();
  const periodKey = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
  const monthStart = `${periodKey}-01`;
  const monthEnd = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0))
    .toISOString().slice(0, 10);
  const { data: budget, error: budgetError } = await admin.from("budgets")
    .select("monthly_limit").eq("user_id", userId).eq("category", category).maybeSingle();
  if (budgetError) return jsonResponse({ error: "Could not read this budget." }, 500);
  if (!budget) return jsonResponse({ status: "no_budget" });

  let spent = 0;
  for (let offset = 0; ; offset += 1000) {
    const { data: rows, error } = await admin.from("transactions")
      .select("amount")
      .eq("user_id", userId)
      .eq("kind", "debit")
      .eq("category", category)
      .gte("transaction_date", monthStart)
      .lte("transaction_date", monthEnd)
      .range(offset, offset + 999);
    if (error) return jsonResponse({ error: "Could not total this month's spending." }, 500);
    spent += (rows || []).reduce((sum, row) => sum + Number(row.amount), 0);
    if (!rows || rows.length < 1000) break;
  }

  const limit = Number(budget.monthly_limit);
  if (spent <= limit) return jsonResponse({ status: "within_budget" });

  const accountSid = Deno.env.get("TWILIO_ACCOUNT_SID") || "";
  const authToken = Deno.env.get("TWILIO_AUTH_TOKEN") || "";
  const sender = Deno.env.get("TWILIO_WHATSAPP_FROM") || "";
  const contentSid = Deno.env.get("TWILIO_WHATSAPP_CONTENT_SID") || "";
  if (![accountSid, authToken, sender, contentSid].every(Boolean)) {
    return jsonResponse({ status: "unconfigured" });
  }

  const { data: event, error: eventError } = await admin.from("alert_events").upsert({
    user_id: userId,
    category,
    period_key: periodKey,
    spent: Number(spent.toFixed(2)),
    monthly_limit: limit,
    status: "pending",
    detail: "",
  }, {
    onConflict: "user_id,category,period_key",
    ignoreDuplicates: true,
  }).select("id").maybeSingle();
  if (eventError) return jsonResponse({ error: "Could not record the alert event." }, 500);
  if (!event) return jsonResponse({ status: "already_sent" });

  const endpoint = `https://api.twilio.com/2010-04-01/Accounts/${accountSid}/Messages.json`;
  const asWhatsapp = (phone: string) => phone.startsWith("whatsapp:") ? phone : `whatsapp:${phone}`;
  const form = new URLSearchParams({
    To: asWhatsapp(profile.whatsapp_phone),
    From: asWhatsapp(sender),
    ContentSid: contentSid,
    ContentVariables: JSON.stringify({
      "1": category,
      "2": `₹${spent.toFixed(2)}`,
      "3": `₹${limit.toFixed(2)}`,
    }),
  });
  let status: "sent" | "failed" = "failed";
  let detail = "WhatsApp alert could not be delivered.";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Basic ${btoa(`${accountSid}:${authToken}`)}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: form,
    });
    status = response.ok ? "sent" : "failed";
    detail = response.ok ? "WhatsApp alert delivered." : `Twilio returned HTTP ${response.status}.`;
  } catch {
    detail = "Could not connect to the WhatsApp provider.";
  }
  await admin.from("alert_events").update({ status, detail }).eq("id", event.id);
  return jsonResponse({ status, spent: Number(spent.toFixed(2)), limit });
}));
