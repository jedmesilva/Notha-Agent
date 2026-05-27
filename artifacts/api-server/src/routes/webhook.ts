import { Router } from "express";

const router = Router();
const NOTHA_URL = "http://localhost:8000";

router.all("/webhook", async (req, res) => {
  try {
    const url = new URL(`${NOTHA_URL}/webhook`);
    for (const [k, v] of Object.entries(req.query)) {
      url.searchParams.set(k, String(v));
    }

    const init: RequestInit = { method: req.method };
    if (req.method === "POST") {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(req.body);
    }

    const pythonRes = await fetch(url.toString(), init);
    const text = await pythonRes.text();
    res.status(pythonRes.status).send(text);
  } catch (err) {
    req.log.error({ err }, "Erro ao contatar servidor Notha");
    res.status(502).json({ error: "Servidor Notha indisponível" });
  }
});

export default router;
