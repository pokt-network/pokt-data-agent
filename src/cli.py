"""CLI interface for the Poktscan agent."""

import sys
import json
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from src.agent import PoktscanAgent


class CLI:
    """Command-line interface for the Poktscan agent."""

    def __init__(self):
        """Initialize the CLI."""
        self.console = Console()
        self.agent = PoktscanAgent()

    def run_interactive(self):
        """Run the CLI in interactive mode."""
        self.console.print(
            Panel(
                "[bold cyan]Poktscan Data Agent[/bold cyan]\n"
                "Ask questions about blockchain data from the Poktscan API.\n"
                "Type 'exit' or 'quit' to stop.",
                title="Welcome",
            )
        )

        while True:
            try:
                self.console.print()
                query = self.console.input("[bold green]Query:[/bold green] ").strip()

                if not query:
                    continue

                if query.lower() in ["exit", "quit"]:
                    self.console.print("[yellow]Goodbye![/yellow]")
                    break

                self.process_query(query)

            except KeyboardInterrupt:
                self.console.print("\n[yellow]Interrupted[/yellow]")
                break
            except Exception as e:
                self.console.print(f"[red]Error: {str(e)}[/red]")

    def process_query(self, query: str):
        """
        Process a single query and display the result.

        Args:
            query: User's natural language query
        """
        # Show processing status
        with self.console.status("[bold cyan]Processing query..."):
            result = self.agent.invoke(query)

        # Display the result
        self._display_result(result)

    def _display_result(self, result: dict):
        """Display the query result to the user."""
        # Check for errors
        if result.get("error"):
            self.console.print(
                Panel(
                    f"[red]{result['error']}[/red]", title="Error", border_style="red"
                )
            )
            return

        # Display the GraphQL query
        if result.get("graphql_query"):
            query_syntax = Syntax(
                result["graphql_query"], "graphql", theme="monokai", line_numbers=False
            )
            self.console.print(
                Panel(query_syntax, title="Generated Query", border_style="blue")
            )

        # Display the result data
        if result.get("result"):
            result_json = json.dumps(result["result"], indent=2)
            result_syntax = Syntax(
                result_json, "json", theme="monokai", line_numbers=False
            )
            self.console.print(
                Panel(result_syntax, title="Result", border_style="green")
            )

        # Display notes if available
        if result.get("notes"):
            self.console.print(f"[dim]{result['notes']}[/dim]")

    def run_single_query(self, query: str):
        """
        Run a single query and exit.

        Args:
            query: User's natural language query
        """
        self.process_query(query)


def main():
    """Main entry point for the CLI."""
    cli = CLI()

    if len(sys.argv) > 1:
        # Single query mode
        query = " ".join(sys.argv[1:])
        cli.run_single_query(query)
    else:
        # Interactive mode
        cli.run_interactive()


if __name__ == "__main__":
    main()
