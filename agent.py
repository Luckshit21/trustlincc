from uagents import Agent

from protocols import bounty_protocol

agent = Agent(name="trustlincc", mailbox=True)
agent.include(bounty_protocol, publish_manifest=True)

if __name__ == "__main__":
    agent.run()