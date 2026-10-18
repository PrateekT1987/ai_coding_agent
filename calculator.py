# calculator.py

def add(a, b):
    """Add two numbers and return the result."""
    return a + b

def subtract(a, b):
    """Subtract b from a and return the result."""
    return a - b

def multiply(a, b):
    """Multiply two numbers and return the result."""
    return a * b

def divide(a, b):
    """Divide a by b and return the result."""
    if b == 0:
        return 'Error: Division by zero'
    return a / b

def main():
    """Run the simple calculator in interactive mode."""
    print('Welcome to the Simple Calculator!')
    while True:
        try:
            num1 = float(input('Enter first number: '))
            op = input('Enter operator (+, -, *, /): ')
            num2 = float(input('Enter second number: '))

            if op == '+':
                result = add(num1, num2)
            elif op == '-':
                result = subtract(num1, num2)
            elif op == '*':
                result = multiply(num1, num2)
            elif op == '/':
                result = divide(num1, num2)
            else:
                print('Invalid operator')
                continue

            print(f'Result: {result}')

            choice = input('Do you want to perform another calculation? (yes/no): ').strip().lower()
            if choice != 'yes':
                print('Exiting the calculator. Goodbye!')
                break
        except ValueError:
            print('Invalid input. Please enter numerical values.')
        except EOFError:
            print('\nExiting the calculator. Goodbye!')
            break

if __name__ == '__main__':
    main()
