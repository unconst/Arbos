# Arbos

![Arbos](arbos.jpg)

All you need is a Ralph loop and a Telegram bot.

## Getting started

```sh
curl -fsSL https://raw.githubusercontent.com/unconst/Arbos/main/run.sh | bash
```

## Next steps

You can just ask the chat how things work
```
How do you work?
```

The main thing is creating agents which run continously on a ralph-loop: calling the same prompt over and over with a delay between calls.
```
# /agent <name> <delay> <prompt>
/agent quant 600 Using my hyperliquid account build a SOTA quant trading system. 
```

You can give them environment vars for tools
```
# /env KEY=VALUE <description>
/env MY_HYPERLIQUID_KEY=******* Secret key to my hyperliquid account.
```

You can send them messages which they get at the beginning of their next loop iteration.
```
# /message <name> <message>
/agent quant Rewrite your ML architecture using the latest timeseries foundation models
```

Or do what ever the hell you want by coding features directly into the bot.
```
# /adapt <prompt>
/adapt I want you to add a new command /pause <agent> which pauses a running agent
```

MIT 

