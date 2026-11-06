import http from "node:http";
import path from "node:path";
import { readFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const pagePath = path.join(root, "pages/guard/index.html");
const bridgePath = fileURLToPath(new URL("./fixture-bridge.js", import.meta.url));

export async function startServer(port = 0) {
  const template = await readFile(pagePath, "utf8");
  const fixture = template.replace(
    "<script>\n(function () {",
    '<script src="/tests/ui/fixture-bridge.js"></script>\n<script>\n(function () {',
  );
  if (fixture === template) throw new Error("无法在 LivingHello 页面中注入测试 bridge");

  const server = http.createServer(async (request, response) => {
    const pathname = new URL(request.url, "http://127.0.0.1").pathname;
    response.setHeader("Cache-Control", "no-store");
    response.setHeader("X-Content-Type-Options", "nosniff");
    if (request.method !== "GET") {
      response.writeHead(405);
      response.end();
      return;
    }
    try {
      if (pathname === "/tests/ui/fixture.html") {
        response.setHeader("Content-Type", "text/html; charset=utf-8");
        response.end(fixture);
      } else if (pathname === "/pages/guard/index.html") {
        response.setHeader("Content-Type", "text/html; charset=utf-8");
        response.end(template);
      } else if (pathname === "/tests/ui/fixture-bridge.js") {
        response.setHeader("Content-Type", "text/javascript; charset=utf-8");
        response.end(await readFile(bridgePath));
      } else {
        response.writeHead(404);
        response.end("Not found");
      }
    } catch (_) {
      response.writeHead(500);
      response.end("Fixture asset error");
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return {
    server,
    url: `http://127.0.0.1:${server.address().port}/tests/ui/fixture.html`,
  };
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const { url } = await startServer(Number(process.env.PORT || 0));
  console.log(`仅隔离 UI fixture：${url}`);
}
