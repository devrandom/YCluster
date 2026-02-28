#!/usr/bin/env python3
"""
Main CLI entry point for YCluster management tools
"""

import sys
import argparse
from .. import __version__


def create_parser():
    """Create the main argument parser"""
    parser = argparse.ArgumentParser(
        prog='ycluster',
        description='YCluster Infrastructure Management Tools',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument(
        '--version', 
        action='version', 
        version=f'ycluster {__version__}'
    )
    
    parser.add_argument(
        '--completion',
        action='store_true',
        help='Generate bash completion script'
    )
    
    subparsers = parser.add_subparsers(
        dest='command',
        help='Available commands',
        metavar='COMMAND'
    )
    
    # Import and register subcommands
    from .cluster import register_cluster_commands
    from .dhcp import register_dhcp_commands
    from .tls import register_tls_commands
    from .https import register_https_commands
    from .certbot import register_certbot_commands
    from .rathole import register_rathole_commands
    from .frontend import register_frontend_commands
    from .backup import register_backup_commands
    from .healthchecks import register_healthchecks_commands
    from .inference import register_inference_commands

    register_cluster_commands(subparsers)
    register_dhcp_commands(subparsers)
    register_tls_commands(subparsers)
    register_https_commands(subparsers)
    register_certbot_commands(subparsers)
    register_rathole_commands(subparsers)
    register_frontend_commands(subparsers)
    register_backup_commands(subparsers)
    register_healthchecks_commands(subparsers)
    register_inference_commands(subparsers)

    return parser


def extract_parser_structure(parser):
    """Extract command structure from argparse parser"""
    structure = {
        'commands': {},
        'options': []
    }
    
    # Get top-level options
    for action in parser._actions:
        if action.option_strings:
            structure['options'].extend(action.option_strings)
    
    # Get subcommands
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice, subparser in action.choices.items():
                structure['commands'][choice] = extract_subparser_structure(subparser)
    
    return structure

def extract_subparser_structure(parser):
    """Extract structure from a subparser"""
    structure = {
        'commands': {},
        'options': [],
        'positional': []
    }
    
    # Get options
    for action in parser._actions:
        if action.option_strings:
            structure['options'].extend(action.option_strings)
        elif action.dest not in ['help', 'func', 'parser'] and not action.dest.endswith('_command'):
            # Positional arguments (excluding special ones)
            structure['positional'].append(action.dest)
    
    # Get subcommands
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice, subparser in action.choices.items():
                structure['commands'][choice] = extract_subparser_structure(subparser)
    
    return structure

def generate_bash_completion_from_structure(structure, cmd_path=[]):
    """Generate bash completion logic from parser structure"""
    lines = []
    
    if not cmd_path:  # Top level
        # Generate main command completion
        main_commands = list(structure['commands'].keys()) + structure['options']
        lines.append(f'            COMPREPLY=($(compgen -W "{" ".join(main_commands)}" -- ${{cur}}))')
        lines.append('            return 0')
    else:
        # Generate subcommand completion
        if structure['commands']:
            subcommands = list(structure['commands'].keys())
            lines.append(f'                    COMPREPLY=($(compgen -W "{" ".join(subcommands)}" -- ${{cur}}))')
        elif structure['options']:
            options = structure['options']
            lines.append(f'                    COMPREPLY=($(compgen -W "{" ".join(options)}" -- ${{cur}}))')
        else:
            lines.append('                    COMPREPLY=()')
        lines.append('                    ;;')
    
    return lines

def generate_completion_script():
    """Generate bash completion script dynamically from argparse structure"""
    parser = create_parser()
    structure = extract_parser_structure(parser)
    
    # Build the completion script
    script_lines = [
        '#!/bin/bash',
        '# Bash completion for ycluster command (auto-generated from argparse)',
        '',
        '_ycluster_completions() {',
        '    local cur prev opts',
        '    COMPREPLY=()',
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        '    ',
        '    case ${COMP_CWORD} in',
        '        1)',
        '            # Complete main commands'
    ]
    
    # Add main command completion
    main_completion = generate_bash_completion_from_structure(structure)
    script_lines.extend(['            ' + line for line in main_completion])
    
    script_lines.extend([
        '            ;;',
        '        2)',
        '            # Complete subcommands based on main command',
        '            case "${COMP_WORDS[1]}" in'
    ])
    
    # Add subcommand completions
    for cmd_name, cmd_structure in structure['commands'].items():
        script_lines.append(f'                {cmd_name})')
        sub_completion = generate_bash_completion_from_structure(cmd_structure, [cmd_name])
        script_lines.extend(['                ' + line for line in sub_completion])
    
    script_lines.extend([
        '            esac',
        '            return 0',
        '            ;;',
        '        3)',
        '            # Complete third-level commands',
        '            case "${COMP_WORDS[1]}" in'
    ])
    
    # Add third-level completions for commands that have them
    for cmd_name, cmd_structure in structure['commands'].items():
        if cmd_structure['commands']:
            script_lines.append(f'                {cmd_name})')
            script_lines.append(f'                    case "${{COMP_WORDS[2]}}" in')
            
            for sub_cmd_name, sub_cmd_structure in cmd_structure['commands'].items():
                if sub_cmd_structure['commands']:
                    script_lines.append(f'                        {sub_cmd_name})')
                    third_level_commands = list(sub_cmd_structure['commands'].keys())
                    if third_level_commands:
                        script_lines.append(f'                            COMPREPLY=($(compgen -W "{" ".join(third_level_commands)}" -- ${{cur}}))')
                    else:
                        script_lines.append('                            COMPREPLY=()')
                    script_lines.append('                            ;;')
            
            script_lines.extend([
                '                        *)',
                '                            COMPREPLY=()',
                '                            ;;',
                '                    esac',
                '                    ;;'
            ])
    
    script_lines.extend([
        '            esac',
        '            return 0',
        '            ;;',
        '        *)',
        '            # Handle file completion for options that expect files',
        '            case "${prev}" in',
        '                --cert-file|--key-file|-k)',
        '                    COMPREPLY=($(compgen -f -- ${cur}))',
        '                    ;;',
        '                --common-name|--remote-addr|--token|--description|url)',
        '                    # These expect string values, no completion',
        '                    COMPREPLY=()',
        '                    ;;',
        '                *)',
        '                    # Default to no completion',
        '                    COMPREPLY=()',
        '                    ;;',
        '            esac',
        '            return 0',
        '            ;;',
        '    esac',
        '}',
        '',
        '# Register the completion function',
        'complete -F _ycluster_completions ycluster'
    ])
    
    return '\n'.join(script_lines)


def main():
    """Main CLI entry point"""
    parser = create_parser()
    args = parser.parse_args()
    
    # Handle completion generation
    if args.completion:
        print(generate_completion_script())
        return
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Execute the command
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
