const pairing = document.querySelector("#pairing");
const image = document.querySelector("#wa-qr");
const guide = document.querySelector("#wa-guide");
const statusLine = document.querySelector("#wa-status");
const labels = {
  offline: "Aguardando o serviço de conexão do TE.",
  waiting: "Preparando a conexão…",
  starting: "Gerando o QR code…",
  pairing: "Aguardando leitura do QR code.",
  error: "OpenWA indisponível. A conexão será verificada novamente.",
  ready: "WhatsApp conectado. O envio dos avisos está ativo.",
};

async function refresh() {
  let delay = 3000;
  try {
    const response = await fetch(pairing.dataset.url, {
      method: "POST", cache: "no-store", redirect: "error",
      headers: { "X-CSRFToken": pairing.querySelector("input").value },
    });
    if (!response.ok) throw new Error();
    const data = await response.json();
    statusLine.textContent = labels[data.status];
    if (data.qr) image.src = data.qr;
    else image.removeAttribute("src");
    image.hidden = guide.hidden = !data.qr;
    if (data.status === "ready") delay = 20000;
  } catch {
    image.hidden = guide.hidden = true;
    image.removeAttribute("src");
    statusLine.textContent = "Não foi possível consultar a conexão. Tentando novamente…";
  }
  window.setTimeout(refresh, delay);
}
refresh();
