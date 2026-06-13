module serial_adder (
    input clk,
    input load,
    input [3:0] a,
    input [3:0] b,
    output sum_bit,
    output [3:0] sum,
    output carry,
    output done
);
    reg [3:0] a_shift;
    reg [3:0] b_shift;
    reg [3:0] sum_shift;
    reg [2:0] count;
    reg carry_r;

    wire partial_sum = a_shift[0] ^ b_shift[0] ^ carry_r;
    wire carry_next = (a_shift[0] & b_shift[0]) |
                      (a_shift[0] & carry_r) |
                      (b_shift[0] & carry_r);

    always @(posedge clk) begin
        if (load) begin
            a_shift <= a;
            b_shift <= b;
            sum_shift <= 4'b0000;
            count <= 3'b000;
            carry_r <= 1'b0;
        end else begin
            a_shift <= {1'b0, a_shift[3:1]};
            b_shift <= {1'b0, b_shift[3:1]};
            sum_shift <= {partial_sum, sum_shift[3:1]};
            carry_r <= carry_next;
            count <= (count == 3'd4) ? count : count + 3'd1;
        end
    end

    assign sum_bit = partial_sum;
    assign sum = sum_shift;
    assign carry = carry_r;
    assign done = (count == 3'd4);
endmodule
