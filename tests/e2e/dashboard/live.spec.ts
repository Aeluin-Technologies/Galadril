import { test, expect, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

test.use({actionTimeout:15_000,navigationTimeout:30_000});

interface Fixture {cookies: Parameters<BrowserContext["addCookies"]>[0]; memberCookies: Parameters<BrowserContext["addCookies"]>[0]; administratorCookies: Parameters<BrowserContext["addCookies"]>[0]; memberId: string; prompt: string; search: string}
const fixture = JSON.parse(readFileSync("live-fixture.json", "utf8")) as Fixture;

function object(value: unknown): Record<string, unknown> {
  const decoded: unknown = value;
  if (!decoded || typeof decoded !== "object" || Array.isArray(decoded))
    throw new Error("Invalid Gateway object");
  return decoded as Record<string, unknown>;
}

async function expectCompletedGeneration(page: Page, context: BrowserContext, deadline: number) {
  const url = new URL(page.url());
  const chatId = url.pathname.split("/").at(-1);
  expect(chatId).toMatch(/^[a-f0-9]{32}$/);
  let status = "running";
  const remaining = deadline - Date.now();
  expect(remaining, "Generation exceeded its existing 90-second budget").toBeGreaterThan(0);
  await expect.poll(async () => {
    const response = await context.request.post(new URL("/graphql", url).href, {
      headers: {origin:url.origin},
      data: {query:"query Completion($id:String!) { pengoloChat(id:$id) { data { activeGeneration messages(limit:1) { items { role status } } } } }",variables:{id:chatId}},
    });
    expect(response.status()).toBe(200);
    const result = object(await response.json());
    expect(result.errors).toBeUndefined();
    const chat = object(object(result.data).pengoloChat);
    const state = object(chat.data);
    expect(state).toHaveProperty("activeGeneration");
    if (state.activeGeneration !== null) return false;
    const messages = object(state.messages).items;
    if (!Array.isArray(messages)) throw new Error("Invalid Gateway messages");
    const message = object(messages.at(-1));
    expect(message.role).toBe("assistant");
    status = String(message.status);
    return ["completed","failed","cancelled"].includes(status);
  }, {timeout:remaining,intervals:[100,250,500]}).toBe(true);
  expect(status).toBe("completed");
}

test("real Pengolo generation survives browser reload", async ({page,context,browser}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.addCookies(fixture.cookies);
  const workspaceResponse = page.waitForResponse(response => {
    const request = response.request();
    if (request.method() !== "POST" || new URL(response.url()).pathname !== "/graphql") return false;
    const query = object(request.postDataJSON()).query;
    return typeof query === "string" && query.includes("query Workspace");
  },{timeout:15_000});
  const [workspace] = await Promise.all([workspaceResponse,page.goto("/pengolo")]);
  expect(workspace.status()).toBe(200);
  const result = object(await workspace.json());
  expect(result.errors).toBeUndefined();
  expect(object(result.data).pengoloModels).toMatchObject({
    defaultModel:"fixture",models:[{alias:"fixture",reasoningEfforts:["medium"]}],
  });
  const catalog = object(object(result.data).pengoloModels);
  if (!Array.isArray(catalog.models)) throw new Error("Invalid model catalog");
  const model = object(catalog.models[0]);
  await expect(page.getByRole("combobox", { name: "Model", exact: true })).toHaveText(String(model.model || model.alias));
  await page.locator(".p-composer").getByLabel("Attach files",{exact:true}).setInputFiles({name:"Evidence.txt",mimeType:"text/plain",buffer:Buffer.from("Confidential operational evidence for the authorized library.")});
  await expect(page.locator(".p-composer")).toContainText("Evidence.txt");
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await expect(page.getByRole("menuitem",{name:"Galadril Graph Context",exact:true})).toHaveCount(0);
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByLabel("Message Pengolo",{exact:true}).fill(fixture.prompt);
  const deadline = Date.now() + 90_000;
  await page.getByRole("button",{name:"Send message",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo\/[a-f0-9]{32}$/);
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).toBeVisible();
  const id = page.url();
  await page.reload();
  await expect(page).toHaveURL(id);
  await expect(page.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"',{timeout:Math.max(1,deadline-Date.now())});
  await expectCompletedGeneration(page, context, deadline);
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).not.toBeVisible();
  await page.setViewportSize({width:390,height:740});
  await expect(page.getByRole("button",{name:"Open Pengolo sidebar",exact:true})).toBeVisible();
  await page.setViewportSize({width:1440,height:1000});
  await page.getByRole("button",{name:"Open Pengolo sidebar",exact:true}).click();
  await expect(page.locator(".p-sidebar")).not.toHaveClass(/closed/);
  await page.getByRole("button",{name:"Projects",exact:true}).hover();
  await page.getByRole("button",{name:"Create project",exact:true}).click();
  const projectDialog = page.getByRole("dialog",{name:"Create project",exact:true});
  await expect(projectDialog).toBeVisible({timeout:15_000});
  await projectDialog.getByLabel("Project name",{exact:true}).fill("Browser shared investigation");
  await projectDialog.getByRole("combobox",{name:/^Access/}).click();
  await page.getByRole("option",{name:"Shared · selected Galadril roles",exact:true}).click();
  await projectDialog.getByRole("textbox",{name:/^Roles/}).fill("pengolo_reader");
  await projectDialog.getByRole("button",{name:"Save project",exact:true}).click();
  const project = page.locator(".p-project").filter({hasText:"Browser shared investigation"});
  await project.locator("summary").click();
  await project.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).click();
  const library = page.getByRole("region",{name:"File library",exact:true});
  await library.locator('input[type="file"]').setInputFiles({name:"Shared evidence.txt",mimeType:"text/plain",buffer:Buffer.from("Evidence shared only with the authorized project role.")});
  await expect(library.getByText("Shared evidence.txt",{exact:true})).toBeVisible();
  if (!await project.getByRole("button",{name:"New chat in Browser shared investigation",exact:true}).isVisible()) await project.locator("summary").click();
  await project.getByRole("button",{name:"New chat in Browser shared investigation",exact:true}).click();
  await page.getByRole("button",{name:"Add files and tools",exact:true}).click();
  await page.getByRole("menuitem",{name:"Choose from library",exact:true}).click();
  await page.getByRole("dialog",{name:"Attach from library",exact:true}).getByRole("button",{name:"Attach",exact:true}).click();
  const sharedPlan:unknown=JSON.parse(fixture.prompt);
  if (typeof sharedPlan!=="object" || sharedPlan===null) throw new Error("Invalid fixture plan");
  await page.getByLabel("Message Pengolo",{exact:true}).fill(JSON.stringify({...sharedPlan,calculator_only:true}));
  const sharedDeadline = Date.now() + 90_000;
  await page.getByRole("button",{name:"Send message",exact:true}).click();
  await expect(page).toHaveURL(/\/pengolo\/[a-f0-9]{32}$/);
  await expect(page.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"',{timeout:Math.max(1,sharedDeadline-Date.now())});
  await expectCompletedGeneration(page, context, sharedDeadline);
  await expect(page.getByRole("button",{name:"Stop generation",exact:true})).not.toBeVisible();
  const sharedUrl=page.url();
  const administratorContext=await browser.newContext();
  await administratorContext.addCookies(fixture.administratorCookies);
  const memberContext=await browser.newContext();
  try {
    await memberContext.addCookies(fixture.memberCookies);
    const member=await memberContext.newPage();
    await member.goto("/pengolo");
    await expect(member.locator(".p-project").filter({hasText:"Browser shared investigation"})).toHaveCount(0);
    for (const forbiddenUrl of [id,sharedUrl]) {
      await member.goto(forbiddenUrl);
      await expect(member.getByRole("alert")).toContainText("no longer available");
      await expect(member.getByRole("article",{name:"Pengolo response"})).toHaveCount(0);
    }
    await member.goto("/pengolo");
    const changeRole=async (operation:string) => {
      const response=await administratorContext.request.post(new URL("/graphql",page.url()).href,{headers:{origin:new URL(page.url()).origin},data:{query:`mutation { administration { ${operation}(userId:"${fixture.memberId}",roleName:"pengolo_reader") } }`}});
      expect(response.status()).toBe(200);
      const result:unknown=await response.json();
      expect(result).not.toHaveProperty("errors");
    };
    await changeRole("assignRoleToUser");
    await member.bringToFront();
    const memberProject=member.locator(".p-project").filter({hasText:"Browser shared investigation"});
    await expect(memberProject).toBeVisible({timeout:25_000});
    await memberProject.locator("summary").click();
    await expect(memberProject.locator(".p-chat-link")).toHaveCount(1);
    await memberProject.locator(".p-chat-link").click();
    await expect(member.getByRole("article",{name:"Pengolo response"})).toContainText('"reasoning_effort":"medium"');
    if (!await memberProject.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).isVisible()) await memberProject.locator("summary").click();
    await memberProject.getByRole("link",{name:"Library for Browser shared investigation",exact:true}).click();
    const memberLibrary = member.getByRole("region",{name:"File library",exact:true});
    await expect(memberLibrary.getByText("Shared evidence.txt",{exact:true})).toBeVisible();
    await memberLibrary.getByRole("button", {name:"Preview Shared evidence.txt", exact:true}).click();
    await expect(member.getByRole("dialog", {name:"Shared evidence.txt", exact:true})).toBeVisible();
    await changeRole("unassignRoleFromUser");
    await member.bringToFront();
    await expect(memberProject).toHaveCount(0,{timeout:25_000});
    await expect(member.getByRole("dialog", {name:"Shared evidence.txt", exact:true})).toHaveCount(0);

    await expect(member.getByText("Shared evidence.txt",{exact:true})).toHaveCount(0);
    await member.goto(sharedUrl);
    await expect(member.getByRole("alert")).toContainText("no longer available");
  } finally { await Promise.all([memberContext.close(),administratorContext.close()]); }
  expect(errors).toEqual([]);
});
