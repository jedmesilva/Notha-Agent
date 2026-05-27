import { Router, type IRouter } from "express";
import healthRouter from "./health";
import webhookRouter from "./webhook";

const router: IRouter = Router();

router.use(healthRouter);
router.use(webhookRouter);

export default router;
